#!/usr/bin/env bash
# bake-chart.sh — issue #183 Phase 3.
#
# Packages charts/spatiumddi-appliance/ into a tgz and drops it into
# the slot rootfs at /usr/lib/spatiumddi/charts/spatiumddi-appliance
# .tgz, where the supervisor's service_lifecycle_k3s module reads
# it at apply time + base64-encodes the bytes into a HelmChart
# CR's spec.chartContent.
#
# Air-gap rationale: the appliance never reaches a chart repo. The
# entire chart ships in the slot image, the supervisor PATCHes it
# inline, k3s's helm-controller renders + applies. No outbound
# network at any point.
#
# Re-runs are cheap — helm package is idempotent for the same chart
# version, and the tgz lands at a stable path so the Makefile's
# slot-build doesn't need extra orchestration.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHART_SRC="$REPO_ROOT/charts/spatiumddi-appliance"
APPLIANCE_DIR="$REPO_ROOT/appliance"
BAKED_DIR="$APPLIANCE_DIR/mkosi.extra/usr/lib/spatiumddi/charts"
BAKED_TARBALL="$BAKED_DIR/spatiumddi-appliance.tgz"

if [ ! -d "$CHART_SRC" ]; then
    echo "ERROR: chart source missing at $CHART_SRC" >&2
    exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
    echo "ERROR: helm CLI not on PATH — install helm to run this bake step" >&2
    echo "       (apt install helm / brew install helm / etc)" >&2
    exit 2
fi

mkdir -p "$BAKED_DIR"

echo "→ Linting chart at $CHART_SRC …"
helm lint "$CHART_SRC" 1>&2

echo "→ Packaging chart → $BAKED_TARBALL …"
# ``helm package`` writes the tarball with a versioned filename
# (e.g. spatiumddi-appliance-0.1.0.tgz). Use --destination + a tmpdir
# so we can rename to the stable canonical path the supervisor reads.
TMP_OUT="$(mktemp -d)"
trap 'rm -rf "$TMP_OUT"' EXIT

helm package "$CHART_SRC" --destination "$TMP_OUT" 1>&2

# helm-package output filename pattern: <name>-<version>.tgz.
# Resolve via glob; the chart directory yields exactly one tgz.
PACKAGED_TGZ="$(ls "$TMP_OUT"/spatiumddi-appliance-*.tgz | head -1)"
if [ ! -f "$PACKAGED_TGZ" ]; then
    echo "ERROR: helm package didn't produce a tgz in $TMP_OUT" >&2
    exit 3
fi

# Stable destination filename so the supervisor's module + the
# Makefile's slot build don't have to chase a versioned suffix.
# Atomic-rename via .new sibling so a crash mid-copy doesn't leave a
# half-written tarball that the supervisor would mis-base64.
cp "$PACKAGED_TGZ" "${BAKED_TARBALL}.new"
mv "${BAKED_TARBALL}.new" "$BAKED_TARBALL"

SIZE=$(du -h "$BAKED_TARBALL" | awk '{print $1}')
echo "✓ Chart baked → ${BAKED_TARBALL} ($SIZE)"
