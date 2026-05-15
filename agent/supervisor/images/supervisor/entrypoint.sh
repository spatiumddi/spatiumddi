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

# Bind-mounted /var/run/docker.sock on the host is owned by root:docker
# (gid varies per distro — 103 on Debian 13, 999 on Alpine builds,
# sometimes other values). The unprivileged ``spatium`` user inside
# the container needs supplementary group membership in that gid to
# read the socket, otherwise every ``docker images`` / ``docker
# inspect`` invocation from heartbeat / lifecycle / capability code
# fails with EACCES and the supervisor silently reports
# ``can_run_dns_bind9=False`` / ``can_run_dhcp=False`` even when the
# baked images are present (which is exactly what would block role
# assignment in the Fleet UI). ``docker exec -u spatium`` papers
# over this by magic-adding the host gid as a supplementary group,
# but ``su-exec`` does not — we have to wire it explicitly.
#
# Strategy: read the socket's gid, ensure /etc/group has a ``docker``
# group at that gid (create or renumber as needed), add spatium to
# it, then re-exec via ``su-exec`` so the supplementary group sticks.
if [ -S /var/run/docker.sock ]; then
    sock_gid=$(stat -c %g /var/run/docker.sock 2>/dev/null || echo "")
    if [ -n "$sock_gid" ] && [ "$sock_gid" != "0" ]; then
        # If a group already exists at this gid, just add spatium to it.
        existing_grp=$(getent group "$sock_gid" 2>/dev/null | cut -d: -f1)
        if [ -z "$existing_grp" ]; then
            # Create a ``docker`` group at the socket's gid. If
            # ``docker`` is already taken at a different gid, delete
            # it first (Alpine's ``addgroup`` won't renumber).
            if getent group docker >/dev/null 2>&1; then
                delgroup docker 2>/dev/null || true
            fi
            addgroup -g "$sock_gid" docker 2>/dev/null || true
            existing_grp=docker
        fi
        # Add spatium to the docker group (idempotent — addgroup
        # noops if already a member).
        addgroup spatium "$existing_grp" 2>/dev/null || true
    fi
fi

# ``su-exec spatium`` (no ``:group`` suffix) calls initgroups() and
# pulls every supplementary group from /etc/group — including the
# ``docker`` group we just added above. Specifying ``spatium:spatium``
# would explicitly set ONE gid (101) and clear everything else,
# silently locking the supervisor out of docker.sock.
exec su-exec spatium /usr/local/bin/spatium-supervisor
