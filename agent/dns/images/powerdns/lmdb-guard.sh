#!/bin/sh
# spatium-pdns-lmdb-guard — snapshot / restore the PowerDNS LMDB across a
# major-version change of the ``pdns_server`` binary (issue #638).
#
# WHY THIS EXISTS
# ---------------
# PowerDNS 5.0 performs an automatic, silent, IRREVERSIBLE LMDB schema
# upgrade (v5 → v6) the first time it opens the database. A read is enough to
# trigger it; there is no opt-out (the documented
# ``lmdb-schema-version=5`` escape hatch is an upstream docs bug: setting
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
#   * when the running binary's MAJOR version is HIGHER than the recorded
#     one, copies the database to ``snapshots/<UTC>-pdns<from>-to-<to>/``
#     BEFORE the daemon gets a chance to open — and therefore migrate — it.
#
# Only an INCREASE is protected. Going back down (5.0 → 4.9) has nothing to
# protect: the on-disk database is already at the newer schema and the older
# binary cannot touch it either way. Snapshotting there would burn disk on
# the recovery path and — because those copies are post-migration — poison
# ``restore latest``.
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
# Which snapshot the live database was last restored from. Makes ``restore``
# idempotent, which matters because ``PDNS_LMDB_RESTORE`` is the documented
# lever for a crash-looping container: without this, every restart would redo
# the restore and stack another full-size undo copy until /var filled.
RESTORED_MARKER="$LMDB_DIR/.pdns-restored-from"
# Head-room required beyond the raw database size, in KiB. 32 MiB of slack
# keeps a snapshot from being the thing that fills the volume the daemon then
# needs to write to.
MARGIN_KB=32768

log() { printf '[lmdb-guard] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# How many snapshots of each kind to keep. Each is a full copy of the
# database, so unbounded retention would eventually fill the volume.
# Validated rather than used raw: POSIX arithmetic resolves a non-numeric word
# as an unset variable, so a typo'd ``KEEP=abc`` would silently become 0 and
# prune the snapshot that was just taken.
KEEP="${PDNS_LMDB_SNAPSHOT_KEEP:-3}"
case "$KEEP" in
    "" | *[!0-9]*)
        log "WARNING: PDNS_LMDB_SNAPSHOT_KEEP=${KEEP} is not a number — using 3"
        KEEP=3
        ;;
esac
[ "$KEEP" -ge 1 ] || KEEP=1

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

# The DATA files the LMDB backend owns: the main env plus the 64 shards
# (``pdns.lmdb-0`` … ``pdns.lmdb-63``, created lazily so only some exist).
#
# ``-lock`` files are deliberately EXCLUDED. They hold LMDB's reader table and
# process-shared mutexes, are recreated on open, and are what ``mdb_copy``
# pointedly never copies — restoring one carrying another process's mutex
# state is a well-known way to wedge an otherwise healthy database.
lmdb_files() {
    find "$LMDB_DIR" -maxdepth 1 -type f -name "${LMDB_BASENAME}*" \
        ! -name '*-lock' 2>/dev/null | sort
}

# Everything including the lock files — used when clearing the live directory
# during a restore, so a stale lock from the replaced database can't survive
# alongside the restored one.
lmdb_files_with_locks() {
    find "$LMDB_DIR" -maxdepth 1 -type f -name "${LMDB_BASENAME}*" 2>/dev/null | sort
}

db_present() {
    [ -s "$LMDB_DIR/$LMDB_BASENAME" ]
}

# APPARENT size of the database, in KiB — deliberately not ``du``.
#
# LMDB preallocates a large sparse map, so ``du`` (allocated blocks) can report
# a fraction of the apparent size. busybox ``cp`` does not preserve holes, so
# the copy lands at the apparent size: budgeting from ``du`` would under-count
# by the entire hole and let a "checked" snapshot run the volume out of space
# mid-copy.
db_size_kb() {
    lmdb_files | tr '\n' '\0' | xargs -0 -r stat -c '%s' 2>/dev/null \
        | awk '{ total += $1 } END { print int((total + 1023) / 1024) }'
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

# Prints a human description of the shortfall when there isn't room for a full
# second copy of the database, or nothing at all when there is. Callers branch
# on whether the output is empty — the exit status is always 0 so ``set -e``
# stays out of it.
space_shortfall() {
    _sf_size="$(db_size_kb)"
    _sf_free="$(avail_kb)"
    _sf_free="${_sf_free:-0}"
    _sf_need=$((_sf_size + MARGIN_KB))
    if [ "$_sf_free" -lt "$_sf_need" ]; then
        printf 'need %sKiB (database %sKiB + %sKiB head-room), have %sKiB on %s' \
            "$_sf_need" "$_sf_size" "$MARGIN_KB" "$_sf_free" "$LMDB_DIR"
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
    # The ``exit 1`` runs in the pipeline's subshell; its status becomes the
    # pipeline's, and therefore this function's, so callers see the failure.
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

# Snapshot names are ``<UTC timestamp>-pdns<from>-to-<to>``. The timestamp
# leads so the shell's lexically-sorted glob is ALWAYS chronological — an
# earlier revision put the versions first, which made "4.9.5-to-5.0.5-…" sort
# before "5.0.5-to-4.9.5-…" regardless of age, so both ``restore latest`` and
# the pruner picked the wrong directory.
#
# ``$1`` selects a kind: ``auto`` (pre-upgrade snapshots) or ``undo``
# (``pre-restore-*``, the undo for a restore). ``*.partial`` staging dirs left
# behind by an interrupted copy are excluded from both — restoring a truncated
# database would be worse than not restoring at all.
snapshot_names() {
    _sn_kind="$1"
    [ -d "$SNAP_ROOT" ] || return 0
    for _sn_d in "$SNAP_ROOT"/*; do
        [ -d "$_sn_d" ] || continue
        _sn_n="${_sn_d##*/}"
        case "$_sn_n" in
            *.partial) continue ;;
            pre-restore-*) [ "$_sn_kind" = "undo" ] || continue ;;
            *) [ "$_sn_kind" = "auto" ] || continue ;;
        esac
        printf '%s ' "$_sn_n"
    done
}

prune_kind() {
    _pk_kind="$1"
    _pk_names="$(snapshot_names "$_pk_kind")"
    _pk_total=0
    for _pk_n in $_pk_names; do
        _pk_total=$((_pk_total + 1))
    done
    _pk_excess=$((_pk_total - KEEP))
    [ "$_pk_excess" -gt 0 ] || return 0
    for _pk_n in $_pk_names; do
        [ "$_pk_excess" -gt 0 ] || break
        log "pruning old snapshot $_pk_n (keep=$KEEP)"
        rm -rf "${SNAP_ROOT:?}/$_pk_n"
        _pk_excess=$((_pk_excess - 1))
    done
}

# ``pre-restore-*`` dirs are bounded too. They used to be exempt on the
# argument that they are the operator's undo, but ``PDNS_LMDB_RESTORE`` on a
# restarting container is exactly the case that generates them in bulk, and an
# unbounded pile fills the partition the daemon writes to. The newest KEEP of
# each kind is what actually stays useful.
prune_snapshots() {
    prune_kind auto
    prune_kind undo
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
        recorded="4.0"
        log "no version marker — assuming this database was written by pdns 4.x"
    fi

    recorded_major="$(major_of "$recorded")"
    running_major="$(major_of "$running")"

    if [ "$recorded_major" = "$running_major" ]; then
        # Same major: no schema migration is possible, nothing to protect.
        # Refresh the marker so a patch-level bump is recorded.
        [ "$recorded" = "$running" ] || log "pdns $recorded → $running (same major, no schema change)"
        printf '%s\n' "$running" > "$MARKER"
        return 0
    fi

    if [ "$running_major" -lt "$recorded_major" ]; then
        # Downgrade. The database is already at the newer schema and this
        # binary cannot open it; a copy would just be another post-migration
        # copy. Say so loudly — this IS the broken state, and the operator
        # needs the restore command, not a snapshot.
        log "pdns $recorded → $running is a DOWNGRADE; not snapshotting"
        log "if this database was migrated by pdns $recorded, this binary cannot open it."
        log "restore the pre-upgrade snapshot:  spatium-pdns-lmdb-guard restore latest"
        printf '%s\n' "$running" > "$MARKER"
        return 0
    fi

    log "pdns major version change: $recorded → $running"
    log "this is a ONE-WAY LMDB schema migration — snapshotting first"

    space_err="$(space_shortfall)"
    if [ -n "$space_err" ]; then
        if [ "${PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE:-0}" = "1" ]; then
            log "WARNING: $space_err — proceeding anyway"
            log "WARNING: PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE=1 — NO ROLLBACK WILL BE POSSIBLE"
            printf '%s\n' "$running" > "$MARKER"
            return 0
        fi
        die "not enough free space to snapshot the LMDB before an irreversible
  schema migration: $space_err.
  Refusing to start pdns $running — opening the database would migrate it and
  there is no downgrade. Free space and restart, redeploy the pdns $recorded
  image, or set PDNS_LMDB_ALLOW_UNPROTECTED_UPGRADE=1 if you have your own
  backup."
    fi

    assert_daemon_stopped "the schema-migration snapshot"

    dest="$SNAP_ROOT/$(date -u '+%Y%m%dT%H%M%SZ')-pdns${recorded}-to-${running}"
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

    log "snapshot written to $dest"
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
        # Glob order is chronological (see snapshot_names), so the last name
        # is the newest.
        want=""
        for _rs_n in $(snapshot_names auto); do
            want="$_rs_n"
        done
        [ -n "$want" ] || die "no snapshots to restore in $SNAP_ROOT"
    fi
    src="$SNAP_ROOT/$want"
    [ -d "$src" ] || die "snapshot '$want' not found in $SNAP_ROOT"
    case "$want" in
        *.partial)
            die "snapshot '$want' is an interrupted copy (.partial) — refusing to restore"
            ;;
    esac
    [ -s "$src/$LMDB_BASENAME" ] || die "snapshot '$want' has no $LMDB_BASENAME — refusing to restore"

    # Idempotent: a container looping on ``PDNS_LMDB_RESTORE`` restores once,
    # not once per restart. Also stops a forgotten env var from silently
    # discarding live changes on every subsequent boot.
    if [ -f "$RESTORED_MARKER" ] \
        && [ "$(head -n 1 "$RESTORED_MARKER" | tr -d ' \t\r\n')" = "$want" ]; then
        log "database was already restored from '$want' — nothing to do"
        return 0
    fi

    assert_daemon_stopped "a restore"

    space_err="$(space_shortfall)"
    if [ -n "$space_err" ]; then
        die "not enough free space to restore safely: $space_err.
  A restore stages the incoming copy and saves the current database first, so
  it needs room for one more copy. Free space, or prune $SNAP_ROOT."
    fi

    # Keep the current database so the restore is itself reversible — an
    # operator who restores the wrong snapshot must not be stuck. Recorded
    # against the MARKER's version, not the running binary's: the marker is
    # what actually wrote these bytes, and a rollback runs this from the OLD
    # image, so using the binary would stamp the undo with a schema it does
    # not contain.
    if db_present; then
        current_version="4.0"
        [ -f "$MARKER" ] && current_version="$(head -n 1 "$MARKER" | tr -d ' \t\r\n')"
        undo="$SNAP_ROOT/pre-restore-$(date -u '+%Y%m%dT%H%M%SZ')"
        log "saving the current database to $undo before restoring"
        copy_db_to "$undo" || die "could not save the current database — aborting restore"
        write_manifest "$undo" "$current_version" "$current_version" "pre-restore-undo"
    fi

    # Stage the incoming copy BEFORE touching the live database. An earlier
    # revision deleted the live files first, so a mid-copy failure left the
    # directory empty — and a subsequent start without the restore env var
    # would have had pdns create a fresh, empty LMDB on top of it.
    staging="$LMDB_DIR/.restore-staging"
    rm -rf "$staging"
    mkdir -p "$staging"
    for f in "$src/$LMDB_BASENAME"*; do
        [ -f "$f" ] || continue
        case "$f" in
            *-lock) continue ;;
        esac
        cp -a "$f" "$staging/" || { rm -rf "$staging"; die "restore copy failed for $f"; }
    done
    [ -s "$staging/$LMDB_BASENAME" ] || { rm -rf "$staging"; die "staged copy of '$want' is empty"; }

    log "restoring snapshot $want"
    lmdb_files_with_locks | while IFS= read -r f; do rm -f "$f"; done
    for f in "$staging/$LMDB_BASENAME"*; do
        [ -f "$f" ] || continue
        mv "$f" "$LMDB_DIR/" || die "could not move $f into place"
    done
    rm -rf "$staging"
    chown -R spatium:spatium "$LMDB_DIR" 2>/dev/null || true
    sync 2>/dev/null || true

    # The restored database is at whatever schema the snapshot was taken at,
    # which the manifest records as "before".
    restored_version="$(sed -n 's/^pdns_version_before=//p' "$src/MANIFEST" 2>/dev/null | head -n 1)"
    if [ -n "$restored_version" ]; then
        log "restore complete — database is back at the pdns $restored_version schema"
    else
        # No manifest: we genuinely do not know what wrote it. Say so rather
        # than asserting a schema, and leave the marker untouched so the next
        # start still evaluates the version change honestly.
        restored_version=""
        log "restore complete — snapshot '$want' has no MANIFEST, schema version unknown"
    fi
    [ -n "$restored_version" ] && printf '%s\n' "$restored_version" > "$MARKER"
    printf '%s\n' "$want" > "$RESTORED_MARKER"

    log "start the matching pdns image; a newer major will migrate it again."
    prune_snapshots
}

usage() {
    cat >&2 <<'EOF'
usage: spatium-pdns-lmdb-guard <command>

  snapshot            snapshot the LMDB if the pdns major version increased
                      (run automatically by the container entrypoint)
  list                list available snapshots with their manifests
  restore [NAME]      restore a snapshot (default: latest). The daemon must
                      be stopped. The current database is saved first.

environment:
  PDNS_LMDB_DIR                          database directory (default /var/lib/powerdns)
  PDNS_LMDB_SNAPSHOT_KEEP                snapshots to retain per kind (default 3)
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
