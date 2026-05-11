# cloud-init NoCloud — first-boot config for the SpatiumDDI appliance

The Phase 1 appliance image ships with the
[NoCloud](https://cloudinit.readthedocs.io/en/latest/reference/datasources/nocloud.html)
cloud-init datasource enabled. Drop a `user-data` + `meta-data` pair
on a CIDATA ISO, attach it as a secondary disk, and the appliance
configures itself on first power-on.

## Build a CIDATA ISO

```sh
cd appliance/cloud-init
cp user-data.example user-data       # edit: SSH key, release pin, hostname
cp meta-data.example meta-data
genisoimage -output cidata.iso -volid CIDATA -joliet -rock user-data meta-data
```

`genisoimage` lives in the `cdrkit` (Debian/Ubuntu) or `cdrtools`
(Alpine) packages. macOS:
```sh
hdiutil makehybrid -o cidata.iso -hfs -joliet -iso \
        -default-volume-name CIDATA cidata-source/
```

## Attach to the VM

| Hypervisor | Recipe |
|---|---|
| **Proxmox** | Upload `cidata.iso` to a storage pool → VM hardware → Add → CD/DVD Drive → ISO image |
| **libvirt / virt-manager** | Add hardware → Storage → CDROM → select cidata.iso |
| **QEMU CLI** | `-drive file=cidata.iso,if=virtio,format=raw,readonly=on` |
| **VMware ESXi** | Datastore browser → upload → VM Edit → CD/DVD → Datastore ISO file |
| **AWS / Azure / GCP** | Cloud-specific datasource handles user-data natively (Phase 5) |

## What `user-data.example` covers

- **Operator account** — creates an `admin` user with `wheel` + `docker`
  groups, sudo NOPASSWD, SSH key. Root SSH login is disabled out of
  the box.
- **Hostname / timezone** — applied before docker comes up.
- **Optional release pin** (`/etc/spatiumddi/release`) — pins
  `SPATIUMDDI_VERSION` to a CalVer tag instead of `:latest`.
- **Optional agent.env** — placeholder for Phase 4 distributed agent
  appliances. Phase 1 is all-in-one; this section is forward-compat.

## What happens on first boot

1. cloud-init runs (`cloud-init-local.service` → `cloud-init.service`
   → `cloud-config.service` → `cloud-final.service`), applies
   user-data: hostname, users, write_files.
2. The `spatiumddi-firstboot.service` systemd unit starts after
   `cloud-final.service` and:
   - generates `/etc/spatiumddi/.env` (POSTGRES_PASSWORD, SECRET_KEY,
     CREDENTIAL_ENCRYPTION_KEY, DNS_AGENT_KEY, DHCP_AGENT_KEY) on
     first run only — preserved across reboots
   - `docker compose pull` (first run) + `docker compose up -d`
   - polls `http://127.0.0.1:8000/health/live` until ready (5 min cap)
3. Web UI is reachable at `http://<appliance-ip>/`. Default login
   `admin` / `admin` (forces password change on first login).

Live progress:
```sh
ssh admin@<appliance-ip>
sudo tail -f /var/log/spatiumddi/firstboot.log
sudo journalctl -u spatiumddi-firstboot -f      # systemd transitions
sudo spatiumddi-stack-status
```
