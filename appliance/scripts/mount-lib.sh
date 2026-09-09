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
# `findmnt` is the proof. build-slot-image.sh's #553 exit trap already
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

unmount_tree() {
    local mp=$1
    mountpoint -q "$mp" 2>/dev/null || return 0
    if ! umount -R "$mp"; then
        echo "  umount -R $mp failed; still mounted underneath it:" >&2
        findmnt -R "$mp" >&2 || true
        echo "  holders:" >&2
        holders_of "$mp" >&2 || true
        echo "  detaching lazily" >&2
        umount -l -R "$mp" || true
    fi
    if [ -n "$(findmnt -R -n -o TARGET "$mp" 2>/dev/null)" ]; then
        echo "ERROR: $mp is still mounted after umount -R and a lazy detach:" >&2
        findmnt -R "$mp" >&2 || true
        return 1
    fi
}
