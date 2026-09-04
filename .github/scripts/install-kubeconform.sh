#!/usr/bin/env bash
#
# Install a pinned, checksum-verified kubeconform (#966).
#
# Shared by the ``Charts — Lint & Template`` CI job and ``make charts-lint``
# (which runs the same gate in a helm container, because the dev box has no
# helm). One pin, one place: bump KUBECONFORM_VERSION here and both move.
#
# Verifies the tarball against the release's CHECKSUMS file before
# extracting — a supply-chain gate on a binary CI downloads on every PR.

set -euo pipefail

KUBECONFORM_VERSION="${KUBECONFORM_VERSION:-v0.8.0}"
BIN_DIR="${KUBECONFORM_BIN_DIR:-/usr/local/bin}"

case "$(uname -m)" in
    x86_64 | amd64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *)
        echo "unsupported architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

tarball="kubeconform-linux-${arch}.tar.gz"
base="https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cd "$tmp"
curl -fsSL -o "$tarball" "${base}/${tarball}"
curl -fsSL -o CHECKSUMS "${base}/CHECKSUMS"
grep -E " ${tarball}\$" CHECKSUMS | sha256sum -c -
tar -xzf "$tarball" kubeconform
install -m 0755 kubeconform "${BIN_DIR}/kubeconform"
"${BIN_DIR}/kubeconform" -v
