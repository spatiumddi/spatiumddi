#!/bin/sh
# Container entrypoint — delegates to the Python supervisor.
# tini is PID 1 (provided by ENTRYPOINT in the Dockerfile).
set -eu

# Issue #170 design pins the supervisor's identity material (Ed25519
# private key + control-plane cert + session token) under an
# unprivileged ``spatium`` user. Defense-in-depth on the keypair —
# the docker.sock + cap_net_admin bind mounts already give effective
# host root, so the unprivileged user isn't a real privilege boundary
# at the container layer, but the python supervisor process running
# unprivileged means an injection-style compromise (rogue HTTP
# payload, deserialisation gadget) can't directly mint a root-owned
# identity file or scribble over /etc/nftables.d contents written by
# host-side runners (SNMP / NTP).
#
# Three bind-mounted paths the unprivileged supervisor needs write
# access to:
#
# * /var/lib/spatium-supervisor → identity key + cert + session
#   token + role-compose.env + role-switch-state. Chowned below.
# * /etc/nftables.d → spatium-role.nft drop-in (the only file the
#   supervisor manages here). Chown the *directory* only; leave
#   existing files (host-managed SNMP / NTP drop-ins) untouched so
#   the unprivileged user can create + atomic-rename its own drop-in
#   without disturbing the host runners' canonical ownership.
# * /var/lib/spatiumddi-host/release-state → trigger files for
#   slot upgrade / reboot / config reloads. The firstboot helper
#   already chmods this to 1777 sticky for the same reason; no
#   chown needed here.
#
# nft itself needs CAP_NET_ADMIN to commit rules. compose grants
# the cap to the container; the Dockerfile's ``setcap
# cap_net_admin+eip /usr/sbin/nft`` propagates it to the binary so
# the unprivileged spatium user can actually use the cap. Same
# pattern as the api image's setcap on /usr/bin/nmap for
# cap_net_raw.

mkdir -p /var/lib/spatium-supervisor
chown -R spatium:spatium /var/lib/spatium-supervisor || true

if [ -d /etc/nftables.d ]; then
    chown spatium:spatium /etc/nftables.d || true
    chmod 0775 /etc/nftables.d || true
fi

exec su-exec spatium:spatium /usr/local/bin/spatium-supervisor
