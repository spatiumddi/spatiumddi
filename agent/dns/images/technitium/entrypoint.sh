#!/bin/sh
# Container entrypoint — delegates to the Python supervisor. tini is PID 1.
set -eu

: "${CONTROL_PLANE_URL:?CONTROL_PLANE_URL is required}"
: "${DNS_AGENT_KEY:?DNS_AGENT_KEY is required (issue #246: pairing-code exchange was removed in #170 Wave A3 — paste the long hex key directly; Application appliances receive it via the supervisor's role-compose.env automatically)}"

# Ensure state + Technitium config dirs are writable by the agent user.
# Unlike bind9/powerdns there is no pre-seeded secret to generate here —
# the Python driver manages its own admin-bootstrap-password + API-token
# files under AGENT_STATE_DIR (see drivers/technitium.py), atomically and
# lazily on first use, so the entrypoint's only job is ownership.
# /var/log/technitium is a hardcoded path in DnsServerCore.LogManager,
# independent of the /etc/dns config dir — the daemon crashes immediately
# on startup ("UnauthorizedAccessException: Access to the path
# '/var/log/technitium' is denied") if it can't create it, since the
# upstream image runs as root and never needed this directory prepared.
mkdir -p /var/lib/spatium-dns-agent /etc/dns /var/log/technitium
chown -R spatium:spatium /var/lib/spatium-dns-agent /etc/dns /var/log/technitium || true

# Supervise: agent process manages the DnsServerApp.dll lifecycle.
#
# Invoked as `python3 -m spatium_dns_agent` rather than the
# `spatium-dns-agent` console-script shim: that shim's shebang is baked in
# by the BUILDER stage's interpreter path (python:3.12-slim-bookworm installs
# to /usr/local/bin/python3), which does not exist in this runtime stage
# (apt installs python3 to /usr/bin/python3) — the mismatch fails with a
# bare "exec failed: no such file or directory" and no other diagnostic.
# bind9/powerdns don't hit this because both their builder AND runtime
# stages are Alpine with apk's python3 at the same path.
exec gosu spatium:spatium python3 -m spatium_dns_agent
