#!/bin/sh
# mount-lib.sh — shared by wrap-iso.sh (bash) and build-slot-image.sh (dash;
# `local` is a dash builtin), which both
# bind-mount the builder's /proc, /sys and /dev into a loop-mounted
# appliance rootfs to chroot `update-initramfs`, and both have to get
# those binds OFF again before they snapshot the tree and remove the
# work directory.
#
#   . "$(dirname "$0")/mount-lib.sh"
#
# unmount_tree <path>
#   Unmount everything mounted at or below <path> — deepest first — and
#   PROVE it is gone before returning 0. Loud on every failure; returns 1
#   (after printing what is still there and who holds it) if the mount
#   survives both a recursive and a lazy detach.
#
# Why this is not just `umount`: after the chroot's update-initramfs the
# bound /sys refuses a plain, non-recursive umount with EBUSY on the
# nightly runners (it did on both architectures of nightly-20260909 and
# in the sandbox replay), and the two scripts handled that in the two
# worst ways available —
#   wrap-iso.sh:  `umount … 2>/dev/null || true` swallowed it, mksquashfs
#                 then walked the builder's live /sys through the bind
#                 ("Failed to read file …/mnt/sys/…, creating empty file"
#                 ×24k), the ISO built and verified, and the exit trap's
#                 `rm -rf "$WORKDIR"` died on the same sysfs — after
#                 unlinking the appliance rootfs on the raw image through
#                 the rw loop mount on its way there. Exit 1, 20 minutes in.
#   build-slot-image.sh: a bare `umount` under set -e: exit 32 at
#                 "Regenerating initrd inside slot…", right after.
# `umount -R` takes sub-mounts down first; a lazy `-l -R` detach is the
# fallback for a mount something still holds open (the tree is gone from
# our namespace either way, which is all mksquashfs, rsync and rm need);
# the mount-table scan is the proof — NOT `findmnt`, which cannot see a mount
# below a plain directory at all. build-slot-image.sh's #553 exit trap already
# had the recursive half of this; wrap-iso.sh never had any of it.

# Processes (in this namespace) whose cwd, root or an open fd is under $1.
holders_of() {
    local under=$1 p target
    for p in /proc/[0-9]*; do
        for target in "$p/cwd" "$p/root" "$p"/fd/*; do
            case "$(readlink "$target" 2>/dev/null)" in
                "$under"|"$under"/*)
                    echo "  pid ${p#/proc/} ($(tr -d '\0' < "$p/comm" 2>/dev/null)) holds $(readlink "$target")"
                    break ;;
            esac
        done
    done
}

# Where the mount table is read from. Overridable ONLY so the path-matching
# below can be tested against a synthetic table without root — every caller
# uses the default.
: "${MOUNT_LIB_MOUNTS_FILE:=/proc/mounts}"

# Canonicalise a path the way /proc/mounts already has. Without this the
# comparison is a literal string match against the kernel's canonical target,
# so a symlinked ancestor (a symlinked TMPDIR is enough) or a trailing slash
# makes every mount invisible — and this helper is BOTH the gate and the
# post-unmount proof, so a miss reads as "nothing mounted, safe to rm -rf".
# Measured: without it, `unmount_tree "$W/link/mnt"` left the tmpfs mounted and
# returned 0, where resolving first clears it.
#
# `realpath -m` does not require the path to exist (it may already be gone by
# the time cleanup runs); `readlink -f` is the fallback, and the raw path the
# last resort so a coreutils-less shell degrades to the old behaviour rather
# than to an exception.
_canon_path() {
    realpath -m "$1" 2>/dev/null \
        || readlink -f "$1" 2>/dev/null \
        || printf '%s' "$1"
}

# Is anything mounted AT or BELOW $1? Reads the mount table rather than asking
# findmnt, because `findmnt -R <path>` resolves <path> to its own mountpoint
# and reports nothing at all when <path> is a plain directory with mounts
# underneath it — so it cannot answer this question, which is the one that
# matters before an `rm -rf`.
anything_mounted_under() {
    local under target
    under=$(_canon_path "$1")
    while read -r _dev target _rest; do
        # /proc/mounts octal-escapes space (\040), tab, newline and backslash.
        # `printf %b` is what turns those back into the path we were handed.
        target=$(printf '%b' "$target")
        case "$target" in
            "$under"|"$under"/*) return 0 ;;
        esac
    done < "$MOUNT_LIB_MOUNTS_FILE"
    return 1
}

# Every mount at or below $1, deepest first — the order umount needs, since a
# parent refuses while a child is mounted on it.
mounts_under() {
    local under target
    under=$(_canon_path "$1")
    while read -r _dev target _rest; do
        target=$(printf '%b' "$target")
        case "$target" in
            "$under"|"$under"/*) printf '%s\t%s\n' "${#target}" "$target" ;;
        esac
    done < "$MOUNT_LIB_MOUNTS_FILE" | sort -rn | cut -f2-
}

unmount_tree() {
    local mp=$1
    # NOT `mountpoint -q || return 0`. That returned SUCCESS for a directory
    # that is not itself a mount but has live mounts beneath it — and the
    # findmnt "proof" below is blind the same way, so the function reported a
    # clean tree while a bind was still up. Harmless at today's call sites
    # (every one is a mount point) and exactly wrong for the next caller who
    # passes $WORKDIR, which is the natural thing for "clean up everything"
    # and what cleanup()'s own "live mounts under it" message already claims
    # to check.
    anything_mounted_under "$mp" || return 0
    mountpoint -q "$mp" 2>/dev/null || {
        # Sub-mounts but no mount at $mp itself: `umount -R` needs a mount
        # point, so walk them deepest-first.
        #
        # The list is RE-READ every pass rather than snapshotted. One
        # `umount -R` can take several entries down at once (stacked mounts at
        # one target, or a subtree), and a stale entry then fails both attempts
        # and latched a failure on a tree that was already clean — reporting
        # ERROR with nothing of our own in the log, while every caller is
        # `|| exit 1`. Bounded so a mount that genuinely will not go never
        # spins; the verdict is the proof below, not the loop.
        local target pass=0
        while [ "$pass" -lt 20 ] && anything_mounted_under "$mp"; do
            pass=$((pass + 1))
            target=$(mounts_under "$mp" | head -n1)
            [ -n "$target" ] || break
            umount -R "$target" 2>/dev/null \
                || umount -l -R "$target" 2>/dev/null \
                || true
        done
        if anything_mounted_under "$mp"; then
            echo "ERROR: mounts remain under $mp after $pass pass(es):" >&2
            mounts_under "$mp" >&2 || true
            holders_of "$mp" >&2 || true
            return 1
        fi
        return 0
    }
    if ! umount -R "$mp"; then
        echo "  umount -R $mp failed; still mounted underneath it:" >&2
        findmnt -R "$mp" >&2 || true
        echo "  holders:" >&2
        holders_of "$mp" >&2 || true
        echo "  detaching lazily" >&2
        umount -l -R "$mp" || true
    fi
    if anything_mounted_under "$mp"; then
        echo "ERROR: $mp is still mounted after umount -R and a lazy detach:" >&2
        mounts_under "$mp" >&2 || true
        holders_of "$mp" >&2 || true
        return 1
    fi
}
