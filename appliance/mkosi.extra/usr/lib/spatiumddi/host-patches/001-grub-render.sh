#!/bin/sh
# Patch 001 — re-render grub.cfg from live partition UUIDs and per-slot
# version labels (#395).
#
# Closes the gap introduced by #393: the spatium_verbose=2 (verbose_dashboard)
# grub conditional branch was added to spatium-grub-render's template but
# could not reach already-installed boxes because grub.cfg is shared on the
# ESP and slot upgrades only applied surgical UUID/label patches. This patch
# invokes the renderer on first boot after a slot upgrade so the full
# three-way conditional (0/1/2) is installed on the ESP without a reinstall.
#
# Secondary effect: re-stamps both slot UUIDs and version labels in the
# rendered grub.cfg, superseding the surgical _patch_grub_cfg_slot_uuid and
# _patch_grub_cfg_slot_label functions for boxes that have the renderer.
#
# Idempotency: spatium-grub-render discovers live UUIDs via lsblk PARTLABEL
# and reads slot-versions.json for labels, then renders atomically with a
# grub-script-check guard. Re-running it is always safe — the output is
# deterministic from the live system state.
#
# Exit 0 = success; non-zero = failure (propagated by exec, so the
# spatium-host-migrate orchestrator stops at this patch and marks it failed
# in the ledger, blocking the trial-boot slot commit).

set -eu
exec /usr/local/bin/spatium-grub-render
