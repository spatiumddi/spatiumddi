#!/bin/sh
# Patch 002 — canonicalise the k3s CIDR drop-in in place (#974).
#
# ``spatium-install`` validated the operator's pod / service CIDRs with
# ``ipaddress.ip_network(..., strict=False)``, which masks host bits for
# the purposes of its overlap checks and then throws the masked value
# away: the literal string typed at the prompt is what reached
# ``/etc/rancher/k3s/config.yaml.d/spatium-cidrs.yaml``. Type
# ``10.42.0.5/16`` and the file recorded a network the cluster never ran.
#
# The installer now stores the canonical form, but that only helps FRESH
# installs. This drop-in is written under the /etc overlay, so it lives on
# the persistent ``/var`` and survives every slot swap — an appliance
# installed before that change carries its literal across the k3s v1.36
# upgrade, which is the population the change is for. Hence a patch.
#
# Why it matters from Kubernetes 1.36 on: ``StrictIPCIDRValidation`` is
# on by default and rejects host-bits-set CIDRs in core API objects, and
# the apiserver derives a ``ServiceCIDR`` object from
# ``--service-cluster-ip-range``. Rather than bet on how that derivation
# normalises, record the form there is no question about.
#
# SAFETY — this cannot change the cluster's networks. Masking host bits
# yields the same network by definition (10.42.0.5/16 and 10.42.0.0/16
# ARE one network), and that masked network is what k3s has been running
# since install. The rewrite makes the file agree with reality; it does
# not migrate anything, which is just as well because k3s does not
# support live CIDR migration. A value that would parse to a DIFFERENT
# network is impossible by construction, and the writer asserts it
# anyway before replacing the file.
#
# For the same reason there is no k3s restart here: nothing about the
# running cluster changes.
#
# Ordering note: spatiumddi-firstboot does not order before k3s.service
# (that would deadlock — see the unit's comment), so on the boot where
# this patch lands k3s may already have started. If a future k3s were to
# refuse the un-canonical flag outright, the A/B trial-boot gate is the
# backstop: firstboot's health commit fails and the node reverts to the
# previous slot.
#
# Idempotent: re-running rewrites nothing once the file is canonical, and
# a missing file is a no-op success (a node that never ran the #302
# installer path has nothing to repair).
#
# Exit 0 = success (including "nothing to do"); non-zero = failure, which
# spatium-host-migrate records in the ledger and which blocks the
# trial-boot slot commit.

set -eu

DROPIN=${SPATIUM_K3S_CIDR_DROPIN:-/etc/rancher/k3s/config.yaml.d/spatium-cidrs.yaml}

[ -f "$DROPIN" ] || exit 0

exec python3 - "$DROPIN" <<'PY'
import ipaddress
import os
import re
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    original = fh.read()

# Only ever the two network-valued keys. ``cluster-dns`` is a single
# address derived from the service CIDR's base and is unaffected by
# masking, and every other line (including the comment header the
# installer wrote) is passed through untouched.
KEYS = ("cluster-cidr", "service-cidr")
pattern = re.compile(r"^(\s*(?:%s)\s*:\s*)(\S+)\s*$" % "|".join(KEYS))

changed = []
out = []
for line in original.splitlines(keepends=True):
    m = pattern.match(line.rstrip("\n"))
    if m is None:
        out.append(line)
        continue
    head, value = m.group(1), m.group(2)
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        # Not parseable — leave it exactly as found. Rewriting a value
        # we do not understand is how a repair becomes an outage.
        out.append(line)
        continue
    canonical = str(net)
    if canonical == value:
        out.append(line)
        continue
    # Belt and braces: the masked value must be the SAME network. This
    # is true by construction; assert it rather than trust the reasoning.
    if ipaddress.ip_network(canonical, strict=False) != net:
        print(f"refusing to rewrite {value!r}: {canonical!r} is a different network",
              file=sys.stderr)
        sys.exit(1)
    changed.append((value, canonical))
    out.append(f"{head}{canonical}\n")

if not changed:
    print("k3s CIDR drop-in already canonical")
    sys.exit(0)

tmp = f"{path}.new"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("".join(out))
os.chmod(tmp, 0o644)
os.replace(tmp, path)
for was, now in changed:
    print(f"k3s CIDR drop-in: {was} -> {now} (same network; host bits masked)")
PY
