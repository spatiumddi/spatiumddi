#!/bin/sh
# spatium-pdns-lmdb-guard — snapshot / restore the PowerDNS LMDB across a
# major-version change of the ``pdns_server`` binary (issue #638).
#
# WHY THIS EXISTS
# ---------------
# PowerDNS 5.0 performs an automatic, silent, IRREVERSIBLE LMDB schema
# upgrade (v5 → v6) the first time it opens the database. A read is enough
# to trigger it; there is no opt-out (the documented
# ``lmdb-schema-version=5`` escape hatch is an upstream docs bug — setting
# it makes pdns refuse to boot outright). Afterwards pdns 4.9 can no longer
# open the database at all:
#
#     Caught an exception instantiating a backend (lmdb), cleaning up
#     Error: Somehow, we are not at schema version 5. Giving up
#
# The LMDB is persisted in every deployment shape (compose named volume /
# appliance hostPath / chart PVC), and the appliance A/B slot rollback swaps
# the ROOTFS while ``/var`` — where the database lives — is the shared
# persistent partition. So without this guard, upgrade-then-rollback leaves
# pdns 4.9 crash-looping on a schema-6 database: DNS down, and the rollback
# cannot recover it. Phase 8c's health-gated auto-revert drives straight
# into that state.
#
# WHAT IT DOES
# ------------
# ``snapshot`` (run from the entrypoint before the agent spawns the daemon):
#   * records which pdns version last owned the database in a marker file;
#   * when the running binary's MAJOR version differs from the recorded one
#     (or the database predates this guard entirely), copies every
#     ``pdns.lmdb*`` file to ``snapshots/<from>-to-<to>-<UTC>/`` BEFORE the
#     daemon gets a chance to open — and therefore migrate — it.
#
# It FAILS CLOSED: if the snapshot cannot be taken (no free space, copy
# error), the container refuses to start rather than letting the daemon burn
# the bridge. A refusal is recoverable by re-deploying the previous image; a
# migrated database is not. ``PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE=1``
# overrides, for operators who have their own backup.
#
# ``restore`` puts a snapshot back. The pre-restore state is itself saved
# first, so a restore is reversible too. This is the operator-facing
# rollback path: rolling back the IMAGE is not enough, the DATABASE has to
# come back with it.
#
# Deliberately POSIX ``sh`` with no Python: it has to work in a container
# whose whole job is failing to start.
set -eu

LMDB_DIR="${PDNS_LMDB_DIR:-/var/lib/powerdns}"
LMDB_BASENAME="${PDNS_LMDB_BASENAME:-pdns.lmdb}"
SNAP_ROOT="$LMDB_DIR/snapshots"
MARKER="$LMDB_DIR/.pdns-version"
# How many automatic snapshots to keep. Each one is a full copy of the
# database, so unbounded retention would eventually fill the volume that
# the daemon needs to write to.
KEEP="${PDNS_LMDB_SNAPSHOT_KEEP:-3}"
# Head-room required beyond the raw database size, in KiB. LMDB files are
# sparse-ish and the copy is not; 32 MiB of slack keeps a snapshot from
# being the thing that fills the volume.
MARGIN_KB=32768

log() { printf '[lmdb-guard] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ``pdns_server --version`` prints e.g.
#   4.9.5:  "Jul 28 12:56:47 PowerDNS Authoritative Server 4.9.5 (C) …"
#   5.0.5:  "PowerDNS Authoritative Server 5.0.5 (C) …"
# on stderr, so the syslog-style timestamp prefix has to be tolerated.
binary_version() {
    pdns_server --version 2>&1 \
        | sed -n 's/.*Authoritative Server \([0-9][0-9.]*\).*/\1/p' \
        | head -n 1
}

major_of() { printf '%s' "${1%%.*}"; }

# Every file the LMDB backend owns: the main env, the 64 shards
# (``pdns.lmdb-0`` … ``pdns.lmdb-63``, created lazily so only some exist),
# and the ``-lock`` reader tables alongside each.
lmdb_files() {
    find "$LMDB_DIR" -maxdepth 1 -type f -name "${LMDB_BASENAME}*" 2>/dev/null | sort
}

db_present() {
    [ -s "$LMDB_DIR/$LMDB_BASENAME" ]
}

# Total size of the database, in KiB.
db_size_kb() {
    lmdb_files | tr '\n' '\0' | xargs -0 -r du -sk 2>/dev/null \
        | awk '{ total += $1 } END { print total + 0 }'
}

avail_kb() {
    df -Pk "$LMDB_DIR" 2>/dev/null | awk 'NR == 2 { print $4 + 0 }'
}

# Refuse to copy a database that a live daemon may be writing to — the copy
# would be torn. The snapshot path runs from the entrypoint before the agent
# spawns pdns_server, so this should never fire; it guards the manual
# invocation.
assert_daemon_stopped() {
    if pgrep -x pdns_server >/dev/null 2>&1; then
        die "pdns_server is running — stop the daemon before ${1:-this operation}"
    fi
}

# NOTE: plain ``sh`` has no function-local variables, so every helper below
# uses a ``_``-prefixed name. Reusing a caller's name here is not a style nit:
# an earlier revision had ``write_manifest`` assign ``dest``, which silently
# rewrote the caller's target path so the snapshot was never promoted out of
# its ``.partial`` staging directory.
copy_db_to() {
    _cp_dest="$1"
    mkdir -p "$_cp_dest"
    lmdb_files | while IFS= read -r _cp_f; do
        cp -a "$_cp_f" "$_cp_dest/" || exit 1
    done
}

write_manifest() {
    _mf_dest="$1"; _mf_from="$2"; _mf_to="$3"; _mf_reason="$4"
    {
        echo "created_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        echo "reason=$_mf_reason"
        echo "pdns_version_before=$_mf_from"
        echo "pdns_version_after=$_mf_to"
        echo "lmdb_dir=$LMDB_DIR"
        echo "size_kb=$(db_size_kb)"
        echo "files:"
        lmdb_files | sed 's|.*/|  |'
    } > "$_mf_dest/MANIFEST"
}

# Automatic snapshot names, oldest first, space-separated. Names are
# ``<from>-to-<to>-<UTC timestamp>`` built from a version regex plus a
# timestamp, so they never contain whitespace and glob order — which the shell
# sorts lexically — is chronological within a version pair.
#
# ``pre-restore-*`` dirs are excluded everywhere: they are the operator's undo
# for a restore they just ran, so they are never auto-pruned and never the
# thing ``restore latest`` picks.
auto_snapshot_names() {
    [ -d "$SNAP_ROOT" ] || return 0
    for _as_d in "$SNAP_ROOT"/*; do
        [ -d "$_as_d" ] || continue
        case "${_as_d##*/}" in
            pre-restore-*) continue ;;
        esac
        printf '%s ' "${_as_d##*/}"
    done
}

prune_snapshots() {
    _pr_names="$(auto_snapshot_names)"
    _pr_total=0
    for _pr_n in $_pr_names; do
        _pr_total=$((_pr_total + 1))
    done
    _pr_excess=$((_pr_total - KEEP))
    [ "$_pr_excess" -gt 0 ] || return 0
    for _pr_n in $_pr_names; do
        [ "$_pr_excess" -gt 0 ] || break
        log "pruning old snapshot $_pr_n (keep=$KEEP)"
        rm -rf "${SNAP_ROOT:?}/$_pr_n"
        _pr_excess=$((_pr_excess - 1))
    done
}

cmd_snapshot() {
    running="$(binary_version)"
    [ -n "$running" ] || die "could not determine pdns_server version"

    if ! db_present; then
        # Fresh install (or the zero-byte leftover the entrypoint cleans
        # up). Nothing to protect — just record who owns it from now on.
        log "no populated LMDB at $LMDB_DIR/$LMDB_BASENAME — recording pdns $running"
        printf '%s\n' "$running" > "$MARKER"
        return 0
    fi

    if [ -f "$MARKER" ]; then
        recorded="$(head -n 1 "$MARKER" | tr -d ' \t\r\n')"
    else
        # A populated database with no marker predates this guard, which
        # only ever shipped on pdns 4.x images. Treat it as 4.x so the
        # first 5.x start is protected.
        recorded="4.x"
        log "no version marker — assuming this database was written by pdns 4.x"
    fi

    if [ "$(major_of "$recorded")" = "$(major_of "$running")" ]; then
        # Same major: no schema migration is possible, nothing to protect.
        # Refresh the marker so a patch-level bump is recorded.
        [ "$recorded" = "$running" ] || log "pdns $recorded → $running (same major, no schema change)"
        printf '%s\n' "$running" > "$MARKER"
        return 0
    fi

    log "pdns major version change: $recorded → $running"
    log "this is a ONE-WAY LMDB schema migration — snapshotting first"

    size_kb="$(db_size_kb)"
    free_kb="$(avail_kb)"
    need_kb=$((size_kb + MARGIN_KB))
    if [ "${free_kb:-0}" -lt "$need_kb" ]; then
        if [ "${PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE:-0}" = "1" ]; then
            log "WARNING: only ${free_kb}KiB free, need ${need_kb}KiB — proceeding"
            log "WARNING: PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE=1 — NO ROLLBACK WILL BE POSSIBLE"
            printf '%s\n' "$running" > "$MARKER"
            return 0
        fi
        die "not enough free space to snapshot the LMDB before an irreversible
  schema migration: need ${need_kb}KiB (database ${size_kb}KiB + ${MARGIN_KB}KiB
  head-room), have ${free_kb:-0}KiB on $LMDB_DIR.
  Refusing to start pdns $running — opening the database would migrate it and
  there is no downgrade. Free space and restart, redeploy the pdns $recorded
  image, or set PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE=1 if you have your own
  backup."
    fi

    assert_daemon_stopped "the schema-migration snapshot"

    dest="$SNAP_ROOT/${recorded}-to-${running}-$(date -u '+%Y%m%dT%H%M%SZ')"
    if [ -e "$dest" ]; then
        log "snapshot $dest already exists — reusing"
    else
        tmp="$dest.partial"
        rm -rf "$tmp"
        if ! copy_db_to "$tmp"; then
            rm -rf "$tmp"
            if [ "${PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE:-0}" = "1" ]; then
                log "WARNING: snapshot copy failed but PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE=1 — proceeding"
                printf '%s\n' "$running" > "$MARKER"
                return 0
            fi
            die "snapshot copy into $tmp failed — refusing to start pdns $running"
        fi
        write_manifest "$tmp" "$recorded" "$running" "pre-upgrade-schema-migration"
        sync 2>/dev/null || true
        mv "$tmp" "$dest"
    fi

    log "snapshot written to $dest (${size_kb}KiB)"
    log "TO ROLL BACK: redeploy the pdns $recorded image AND run"
    log "  spatium-pdns-lmdb-guard restore latest"
    log "rolling back the image alone will leave pdns $recorded crash-looping."

    printf '%s\n' "$running" > "$MARKER"
    prune_snapshots
}

cmd_list() {
    if [ ! -d "$SNAP_ROOT" ] || [ -z "$(ls -A "$SNAP_ROOT" 2>/dev/null)" ]; then
        echo "no snapshots in $SNAP_ROOT"
        return 0
    fi
    for d in "$SNAP_ROOT"/*; do
        [ -d "$d" ] || continue
        printf '%s\n' "$(basename "$d")"
        if [ -f "$d/MANIFEST" ]; then
            sed 's/^/    /' "$d/MANIFEST"
        fi
    done
}

cmd_restore() {
    want="${1:-latest}"
    [ -d "$SNAP_ROOT" ] || die "no snapshots directory at $SNAP_ROOT"

    if [ "$want" = "latest" ]; then
        # Glob order is chronological (see auto_snapshot_names), so the last
        # name wins.
        want=""
        for _rs_n in $(auto_snapshot_names); do
            want="$_rs_n"
        done
        [ -n "$want" ] || die "no snapshots to restore in $SNAP_ROOT"
    fi
    src="$SNAP_ROOT/$want"
    [ -d "$src" ] || die "snapshot '$want' not found in $SNAP_ROOT"
    [ -s "$src/$LMDB_BASENAME" ] || die "snapshot '$want' has no $LMDB_BASENAME — refusing to restore"

    assert_daemon_stopped "a restore"

    # Keep the current database so the restore is itself reversible — an
    # operator who restores the wrong snapshot must not be stuck.
    if db_present; then
        undo="$SNAP_ROOT/pre-restore-$(date -u '+%Y%m%dT%H%M%SZ')"
        log "saving the current database to $undo before restoring"
        copy_db_to "$undo" || die "could not save the current database — aborting restore"
        write_manifest "$undo" "$(binary_version)" "$(binary_version)" "pre-restore-undo"
    fi

    log "restoring snapshot $want"
    lmdb_files | while IFS= read -r f; do rm -f "$f"; done
    for f in "$src/$LMDB_BASENAME"*; do
        [ -f "$f" ] || continue
        cp -a "$f" "$LMDB_DIR/" || die "restore copy failed for $f"
    done
    chown -R spatium:spatium "$LMDB_DIR" 2>/dev/null || true
    sync 2>/dev/null || true

    # The restored database is at whatever schema the snapshot was taken
    # at, which is the version recorded in the manifest as "before".
    restored_version="$(sed -n 's/^pdns_version_before=//p' "$src/MANIFEST" 2>/dev/null | head -n 1)"
    [ -n "$restored_version" ] || restored_version="4.x"
    printf '%s\n' "$restored_version" > "$MARKER"

    log "restore complete — database is back at the pdns $restored_version schema"
    log "start the pdns $restored_version image; a newer image will migrate it again."
}

usage() {
    cat >&2 <<'EOF'
usage: spatium-pdns-lmdb-guard <command>

  snapshot            snapshot the LMDB if the pdns major version changed
                      (run automatically by the container entrypoint)
  list                list available snapshots with their manifests
  restore [NAME]      restore a snapshot (default: latest). The daemon must
                      be stopped. The current database is saved first.

environment:
  PDNS_LMDB_DIR                          database directory (default /var/lib/powerdns)
  PDNS_LMDB_SNAPSHOT_KEEP                automatic snapshots to retain (default 3)
  PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE=1  proceed even if the snapshot fails
  PDNS_LMDB_RESTORE=<NAME|latest>        entrypoint restores before starting
EOF
    exit 2
}

case "${1:-}" in
    snapshot) cmd_snapshot ;;
    list) cmd_list ;;
    restore) shift; cmd_restore "${1:-latest}" ;;
    *) usage ;;
esac
