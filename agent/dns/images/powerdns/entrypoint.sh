#!/bin/sh
# Container entrypoint — delegates to the Python supervisor. tini is PID 1.
set -eu

: "${CONTROL_PLANE_URL:?CONTROL_PLANE_URL is required}"
: "${DNS_AGENT_KEY:?DNS_AGENT_KEY is required (issue #246: pairing-code exchange was removed in #170 Wave A3 — paste the long hex key directly; Application appliances receive it via the supervisor's role-compose.env automatically)}"

# Ensure state + LMDB dirs are writable by the agent user.
mkdir -p /var/lib/spatium-dns-agent /var/lib/powerdns
chown -R spatium:spatium /var/lib/spatium-dns-agent /var/lib/powerdns || true

# PowerDNS REST API key. Generate once and keep stable across
# container restarts so the agent and pdns_server stay in sync.
# 32 random url-safe bytes is well-padded for the API surface.
API_KEY_FILE=/var/lib/spatium-dns-agent/pdns-api.key
if [ ! -f "$API_KEY_FILE" ]; then
    head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | head -c 32 > "$API_KEY_FILE"
    chmod 600 "$API_KEY_FILE"
fi
chown spatium:spatium "$API_KEY_FILE" || true

# LMDB backend self-initialises on first ``pdns_server`` start —
# the daemon mmaps the configured ``lmdb-filename`` (and its sharded
# siblings ``pdns.lmdb-0`` … ``pdns.lmdb-63`` per ``lmdb-shards``)
# and creates them with the right env header. We deliberately do
# NOT pre-create the file: an empty 0-byte ``pdns.lmdb`` is rejected
# by ``mdb_env_open`` with "STL Exception while filling the zone
# cache" because LMDB requires a non-empty env header to mmap.
# ``pdnsutil create-bind-db`` is the wrong helper for this backend
# (it's the BIND-backend SQL bootstrap; LMDB has no equivalent — the
# daemon owns the file format on first open).
#
# If a stale empty / partial file is left over from a prior bad
# start, remove it so the next start gets a clean LMDB env. The
# unconditional cleanup is safe because LMDB pre-creation was never
# correct — operators with real zone data on this volume already
# have a properly-sized file that we won't touch (size > 0 means
# pdns has fully initialised the env, so we leave it alone).
if [ -f /var/lib/powerdns/pdns.lmdb ] \
    && [ ! -s /var/lib/powerdns/pdns.lmdb ]; then
    rm -f /var/lib/powerdns/pdns.lmdb /var/lib/powerdns/pdns.lmdb-lock
fi

# Issue #638 — LMDB schema guard. PowerDNS 5.0 silently and irreversibly
# migrates the LMDB schema (v5 → v6) the first time it opens the database, so
# a plain image rollback afterwards leaves the older pdns crash-looping on a
# database it can no longer read. Both steps below run BEFORE the exec below,
# because the agent spawns pdns_server and that spawn is the point of no
# return.
#
# ``PDNS_LMDB_RESTORE`` is the operator's rollback lever: set it to ``latest``
# (or a specific snapshot name from ``spatium-pdns-lmdb-guard list``) when
# redeploying an older image, and the database is put back to the schema that
# image understands before the daemon starts. It is deliberately one-shot in
# spirit — leaving it set means every restart re-restores and discards live
# changes — so the guard logs loudly and the docs say to unset it after the
# node is healthy.
if [ -n "${PDNS_LMDB_RESTORE:-}" ]; then
    echo "PDNS_LMDB_RESTORE=${PDNS_LMDB_RESTORE} — restoring the LMDB before start" >&2
    /usr/local/bin/spatium-pdns-lmdb-guard restore "$PDNS_LMDB_RESTORE"
fi

# Snapshots the database when the pdns major version changed since the last
# start. Fails closed: a container that refuses to start can be recovered by
# redeploying the previous image, a migrated database cannot.
/usr/local/bin/spatium-pdns-lmdb-guard snapshot
chown -R spatium:spatium /var/lib/powerdns || true

# Supervise: agent process manages pdns_server lifecycle.
exec su-exec spatium:spatium /usr/local/bin/spatium-dns-agent
