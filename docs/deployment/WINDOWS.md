---
layout: default
title: Windows Server Setup
---

# Windows Server Setup (DNS + DHCP)

SpatiumDDI can manage Windows Server DNS and DHCP agentlessly — no software installed on the Windows side. DNS has two tiers (RFC 2136 always-on, WinRM unlocks more); DHCP has one tier today (WinRM read-only lease mirroring).

This page is the **Windows-side checklist**. The SpatiumDDI-side config is covered in [features/DNS.md §13](../features/DNS.md#13-windows-dns-path-a--b) and [features/DHCP.md §15](../features/DHCP.md#15-windows-dhcp-path-a-read-only).

> **You do not install anything on the Windows server.** Everything runs remotely over WinRM (DNS zone management + DHCP reads) or RFC 2136 / dnspython over UDP/TCP 53 (DNS record writes + AXFR).

---

## TL;DR — the minimum

Both DNS Path B and DHCP Path A use WinRM, so they share most of the setup:

1. **Enable WinRM** on the Windows server (`winrm quickconfig` — usually already on for domain controllers).
2. **Open the firewall** — TCP 5985 (HTTP) or 5986 (HTTPS) from the SpatiumDDI host.
3. **Create a service account** in the right security group:
   - DNS Path B → in `DnsAdmins` on the DC (or a delegated group with the same DNS rights).
   - DHCP Path A → in `DHCP Users` (read only).
4. **Configure the account in SpatiumDDI** when you add the server — username / password / transport (`ntlm` recommended for domain-joined, `basic` if you must, `kerberos` if you run the AD side).
5. (DNS only) **Enable dynamic updates** on each zone you want SpatiumDDI to write records to — **Nonsecure and secure** for Path A's unsigned RFC 2136, or **Secure only** if you're using Path B exclusively for zone management and don't need per-record writes.
6. (DNS Path A only) **Allow AXFR** from the SpatiumDDI host, or use Path B (WinRM) to sidestep AXFR entirely.

Test from the SpatiumDDI UI with the **Test Connection** button on the server create form before saving.

---

## 1. WinRM prerequisites

### On the Windows server

WinRM is usually already enabled on domain controllers. If not:

```powershell
winrm quickconfig
winrm set winrm/config/service/Auth '@{Basic="true"}'     # only if using basic auth
winrm set winrm/config/service '@{AllowUnencrypted="true"}' # only if using HTTP + basic; prefer HTTPS
```

Check what's listening:

```powershell
winrm enumerate winrm/config/listener
```

You want a listener on port **5985** (HTTP) or **5986** (HTTPS). SpatiumDDI prefers HTTPS — use HTTP only on isolated management networks.

### Transport choices

| Transport | Port | Cert needed? | When to use |
|---|---|---|---|
| `ntlm` | 5985 or 5986 | No | Domain-joined AD environments — default. Works from Linux via `pywinrm`. |
| `kerberos` | 5985 or 5986 | No (but needs Kerberos tickets) | If the SpatiumDDI host is domain-joined and running `kinit`. Not typical. |
| `basic` | 5985 or 5986 | Recommended HTTPS | Non-domain use. Requires `AllowUnencrypted=true` on HTTP — avoid. |
| `credssp` | 5985 or 5986 | Yes | Double-hop scenarios (rarely needed for DNS/DHCP). |

SpatiumDDI stores these on `DNSServer.credentials_encrypted` / `DHCPServer.credentials_encrypted` as a Fernet-encrypted dict:

```json
{
  "username": "CORP\\spatium-dns",
  "password": "…",
  "winrm_port": 5986,
  "transport": "ntlm",
  "use_tls": true,
  "verify_tls": true
}
```

`verify_tls: false` is acceptable for self-signed WinRM certs; it's a per-server setting so you can opt-out per host without globally disabling verification.

### Firewall

Allow inbound TCP 5985/5986 from the SpatiumDDI host only:

```powershell
New-NetFirewallRule -DisplayName "SpatiumDDI WinRM HTTPS" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5986 `
  -RemoteAddress <spatium-host-or-subnet>
```

For DNS Path A (RFC 2136 + AXFR) you also need UDP + TCP 53 from the SpatiumDDI host.

---

## 2. DNS — Windows Server side

### Path A (RFC 2136, no credentials)

This is the baseline — no WinRM needed, just record-level RFC 2136 dynamic updates.

**On each zone you want SpatiumDDI to write to:**

1. Open **DNS Manager** on the DC.
2. Right-click the zone → **Properties** → **General** tab.
3. Set **Dynamic updates** to **Nonsecure and secure**.
   > **Note:** AD-integrated zones default to "Secure only", which rejects unsigned RFC 2136. Either change it, or use Path B (WinRM) for zone management and accept that record writes will fail until you also enable Nonsecure. See "Secure-only zones" below for the GSS-TSIG path.

4. **Zone transfers** tab → allow transfers to the SpatiumDDI host's IP (this enables AXFR for `pull_zone_records`).

That's it. Create a `windows_dns` server in SpatiumDDI **without credentials** pointing at the DC's IP; it will drive the zone via `dnspython` over port 53.

### Path B (WinRM + PowerShell, credentials required)

Path B adds zone create/delete and a WinRM-based zone record pull (which sidesteps AXFR ACLs on AD-integrated zones).

**On the DC:**

1. Make sure WinRM is reachable (§1 above).
2. Create a service account (an AD user, not a local user on the DC):
   ```
   New-ADUser -Name "spatium-dns" -SamAccountName "spatium-dns" `
     -AccountPassword (Read-Host -AsSecureString) -Enabled $true `
     -UserPrincipalName "spatium-dns@corp.example.com"
   ```
3. Add it to `DnsAdmins`:
   ```
   Add-ADGroupMember -Identity DnsAdmins -Members spatium-dns
   ```
   `DnsAdmins` is enough for `Add-DnsServerPrimaryZone`, `Remove-DnsServerZone`, and `Get-DnsServerResourceRecord`.

4. **Don't skip the zone dynamic-update setting.** Path B uses WinRM for zone topology, but record-level writes still go over RFC 2136. If the zone is "Secure only", record writes fail — same as Path A.

**On the SpatiumDDI side:**

When you add the server in **DNS → Server Groups → Add Server**, fill in:

- **Host** — the DC's FQDN or IP
- **Driver** — `windows_dns`
- **WinRM credentials** section — username (use `DOMAIN\user` or `user@corp.example.com`), password, port (5985 or 5986), transport, TLS options.

Click **Test Connection** to run a `(Get-DnsServerSetting -All).BuildNumber` probe before saving. Green = Path B is live. Red = check the error; common failures are firewall, bad transport (try `ntlm` instead of `basic`), or the account not being in `DnsAdmins`.

### Secure-only zones (GSS-TSIG — future)

AD-integrated zones in "Secure only" mode require GSS-TSIG (Kerberos-signed RFC 2136). SpatiumDDI doesn't implement GSS-TSIG yet — it's on the roadmap. Today's options:

- Change the zone to "Nonsecure and secure".
- Or, manage the zone via Path B (zone CRUD only) and skip per-record writes.

---

## 3. DHCP — Windows Server side

### Path A (WinRM, read-only)

Only read-only today — SpatiumDDI polls leases and mirrors them into IPAM, but doesn't push scopes or reservations to Windows DHCP. Path B (full CRUD) is on the roadmap.

**On the DHCP server:**

1. WinRM reachable (§1 above).
2. Create an AD service account (same as DNS — but a separate account if you want to scope permissions independently).
3. Add it to the **DHCP Users** local group on the DHCP server:
   ```
   # On the DHCP server, not the DC
   Add-LocalGroupMember -Group "DHCP Users" -Member "CORP\spatium-dhcp"
   ```
   Path B (future) will need `DHCP Administrators` instead.

4. No other configuration needed — `Get-DhcpServerv4Scope` / `Get-DhcpServerv4Lease` work out of the box.

**On the SpatiumDDI side:**

- **DHCP → Server Groups → Add Server**
- **Driver** — `windows_dhcp`
- **WinRM credentials** — same shape as the DNS Path B credentials.
- **Test Connection** runs `Get-DhcpServerSetting` to verify.

Then enable **DHCP Lease Sync** in **Settings**. Beat fires every 60s; the task gates on the enabled toggle + interval (default 5 minutes), so you can change cadence without restarting anything.

Each lease upserts by `(server_id, ip_address)` and mirrors into IPAM as a row with `status="dhcp"` and `auto_from_lease=True` — the existing lease-cleanup sweep handles expiry uniformly.

---

## 4. Diagnosing problems from Linux

Test WinRM reachability independently of SpatiumDDI:

```bash
# From the SpatiumDDI host (or any Linux box with python3):
pip install pywinrm
python3 - <<'PY'
import winrm
s = winrm.Session(
    "https://dc01.corp.example.com:5986",
    auth=("CORP\\spatium-dns", "…"),
    transport="ntlm",
    server_cert_validation="ignore",
)
r = s.run_ps("(Get-DnsServerSetting -All).BuildNumber")
print("stdout:", r.std_out.decode())
print("stderr:", r.std_err.decode())
print("rc:", r.status_code)
PY
```

Common failures:

| Symptom | Cause |
|---|---|
| `WinRMTransportError: 401` | Wrong username/password, wrong transport, or `AllowUnencrypted=false` with `use_tls: false`. |
| `WinRMTransportError: 500 ... Access is denied.` | Account is authenticated but not in `DnsAdmins` / `DHCP Users`. |
| Connection timeout | Firewall, wrong port, or WinRM listener not running. |
| `SSL CERTIFICATE_VERIFY_FAILED` | Self-signed WinRM cert — set `verify_tls: false` on the credentials. |

For DNS Path A (RFC 2136), check the zone's "Dynamic updates" setting and try `nsupdate` by hand from the SpatiumDDI host:

```bash
nsupdate -d <<EOF
server dc01.corp.example.com
zone corp.example.com.
update add test.corp.example.com. 60 A 10.1.2.3
send
EOF
```

A `REFUSED` response points at the dynamic-updates setting; a `NOTAUTH` points at the zone's primary NS value in SpatiumDDI not matching the server.

---

## 5. Hardening checklist (production)

- [ ] Use **HTTPS WinRM** (port 5986) with a cert from your internal CA — verify it with `verify_tls: true`.
- [ ] Dedicated service accounts — one for DNS (`DnsAdmins`), one for DHCP (`DHCP Users`) — not Domain Admins.
- [ ] Firewall inbound 5985/5986 from the SpatiumDDI host only — `Get-NetFirewallRule`.
- [ ] For DNS Path A, restrict AXFR to the SpatiumDDI host IP (zone **Zone Transfers** tab → "Only to the following servers").
- [ ] Rotate the service account password on a schedule — SpatiumDDI re-encrypts when you save the server, no restart needed.
- [ ] Audit WinRM access via Windows event log (`Microsoft-Windows-WinRM/Operational`).
- [ ] Disable unused transports on the WinRM listener (don't leave `Basic` on if you're using NTLM).

---

## Related docs

- [Getting Started](../GETTING_STARTED.md) — setup order: servers → zones/scopes → subnets → addresses.
- [DNS Features](../features/DNS.md) — zones, records, views, sync jobs.
- [DHCP Features](../features/DHCP.md) — scopes, pools, leases.
- [DNS Drivers](../drivers/DNS_DRIVERS.md) — driver internals including `WindowsDNSDriver`.
- [DHCP Drivers](../drivers/DHCP_DRIVERS.md) — driver internals including `WindowsDHCPReadOnlyDriver`.
