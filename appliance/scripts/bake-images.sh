#!/usr/bin/env bash
# bake-images.sh — issue #183 Phase 7.
#
# Replaces the pre-Phase-7 docker-overlay.img approach. The k3s
# appliance preloads its container images by dropping ``*.tar.zst``
# archives into /var/lib/rancher/k3s/agent/images/. k3s scans that
# directory at startup and imports anything new into containerd —
# native, no firstboot shell-out needed.
#
# We tag each image as ``ghcr.io/spatiumddi/<name>:${SPATIUMDDI_VERSION}``
# before save so the imported image matches the reference the chart's
# values.yaml uses. SPATIUMDDI_VERSION resolves from the Makefile (CI
# release sets it to the CalVer tag; local dev gets ``dev-<short-sha>-
# <rand>``).
#
# Also writes /usr/lib/spatiumddi/spatiumddi-version so firstboot can
# sync .env's SPATIUMDDI_VERSION line to the baked tag — without that
# sidecar the chart's image reference would resolve to ``:latest``
# which doesn't exist in containerd, and pods would CrashLoopBackOff
# with ``ErrImagePull`` (air-gap: fatal).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLIANCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$APPLIANCE_DIR/.." && pwd)"
IMAGES_DIR="$APPLIANCE_DIR/mkosi.extra/var/lib/rancher/k3s/agent/images"
VERSION_FILE="$APPLIANCE_DIR/mkosi.extra/usr/lib/spatiumddi/spatiumddi-version"

SPATIUMDDI_VERSION="${SPATIUMDDI_VERSION:-dev}"
BAKE_SOURCE="${BAKE_SOURCE:-local}"  # local (docker :dev) | ghcr (pull from ghcr.io)

# SpatiumDDI service images. Tagged with SPATIUMDDI_VERSION so the
# chart's ``image: ghcr.io/spatiumddi/<name>:${SPATIUMDDI_VERSION}``
# reference resolves locally without a pull.
IMAGES=(
    "ghcr.io/spatiumddi/spatium-supervisor"
    "ghcr.io/spatiumddi/dns-bind9"
    "ghcr.io/spatiumddi/dns-powerdns"
    "ghcr.io/spatiumddi/dhcp-kea"
    # Phase 11 (#183) — control-plane images for the AIO + Core
    # install variants. Application-role appliances don't run these
    # pods, but baking them keeps the slot consistent across all
    # three variants (no "I built a Core but my image set was for
    # Application" gotcha) — disk cost ~250 MB extra on the slot.
    #
    # NOTE — only ``spatiumddi-api`` + ``spatiumddi-frontend`` are
    # separately-built images. The umbrella chart's worker / beat /
    # migrate Deployments + Jobs all run the SAME spatiumddi-api
    # image with different ``command:`` overrides (verified across
    # docker-compose.yml + charts/spatiumddi/templates/{api,worker,
    # beat,migrate}.yaml). Don't add ``spatiumddi-worker`` /
    # ``-beat`` / ``-migrate`` here — they don't exist in ghcr.
    "ghcr.io/spatiumddi/spatiumddi-api"
    "ghcr.io/spatiumddi/spatiumddi-frontend"
)

# Issue #183 Phase 8 — 3rd-party observability images. Tagged with
# the upstream version (NOT SPATIUMDDI_VERSION) since they're
# distributed-as-binaries upstream. The chart references them with
# their canonical names (``registry.k8s.io/kube-state-metrics/...:
# v2.13.0`` etc); we pull + save without retag.
#
# Format: ``<full-image>:<tag>``. Keep in lock-step with
# ``charts/spatiumddi-appliance/values.yaml`` ``observability.*``
# image refs.
OBSERVABILITY_IMAGES=(
    "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.18.0"
    "quay.io/prometheus/node-exporter:v1.11.1"
    # Agent landing page — always-on nginx serving the rendered
    # /var/lib/spatiumddi/agent-landing/index.html on :80. Pinned to
    # 1.30.1-alpine matching values.yaml's ``agentLanding.image.tag``.
    "nginx:1.30.1-alpine"
    # Phase 11 (#183) — Redis datastore for the control plane.
    # Tag follows the umbrella chart's ``redis.image.tag`` default.
    # NOTE: the standalone ``postgres:16-alpine`` image is intentionally
    # NOT baked anymore (#277). The appliance now runs PostgreSQL under
    # CloudNativePG on every install (single instance → 3/5/7 on
    # promote); the CNPG runtime image (ghcr.io/cloudnative-pg/postgresql)
    # + operator are baked in CNPG_IMAGES below. The chart's standalone
    # StatefulSet path stays for non-appliance docker/k8s users, but the
    # appliance never renders it, so its image is dead weight in the ISO.
    "redis:8.6-alpine"
)

# #272 Phase 5 — MetalLB (control-plane HTTPS VIP + Phase 10 DNS/DHCP
# VIPs). Official upstream images (NOT bitnami), tagged with the chart
# appVersion. Baked so multi-node HA promotion works in air-gapped
# environments with zero outbound pulls — same as every other image.
#
# L2 (native) mode only: ``metallb.speaker.frr.enabled=false`` AND
# ``metallb.frrk8s.enabled=false`` in charts/spatiumddi-appliance/
# values.yaml (0.16.0 made frr-k8s the default BGP backend), so the
# quay.io/metallb/frr-k8s + quay.io/frrouting/frr images are
# intentionally NOT baked. If/when BGP mode lands (Phase 10), enable
# frrk8s + add those two images here.
#
# KEEP these refs in lock-step with the metallb.controller.image /
# metallb.speaker.image pins in the appliance chart's values.yaml.
METALLB_IMAGES=(
    "quay.io/metallb/controller:v0.16.0"
    "quay.io/metallb/speaker:v0.16.0"
)

# CloudNativePG (#272 / #277) — PostgreSQL is CNPG on every appliance
# (single-node = 1 instance, scales to 3/5/7 on control-plane promote).
# The operator runs from the spatiumddi-appliance chart's cnpg subchart;
# the runtime image is what each Cluster instance pod runs. Both MUST be
# baked or a fresh airgap install can't bring postgres up.
#   * operator tag = cloudnative-pg chart 0.28.2 appVersion (1.29.1).
#   * runtime tag  = charts/spatiumddi values.yaml postgresql.cnpg.imageName.
# KEEP in lock-step with charts/spatiumddi-appliance/charts/cloudnative-pg-*.tgz
# (appVersion) and charts/spatiumddi/values.yaml (cnpg.imageName).
CNPG_IMAGES=(
    "ghcr.io/cloudnative-pg/cloudnative-pg:1.29.1"
    "ghcr.io/cloudnative-pg/postgresql:16"
)

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker CLI required on the build host (used to save + retag images)." >&2
    echo "       The APPLIANCE itself ships zero docker; this is build-host tooling only." >&2
    exit 2
fi
if ! command -v zstd >/dev/null 2>&1; then
    echo "ERROR: zstd required on the build host (compresses image archives)." >&2
    exit 2
fi

mkdir -p "$IMAGES_DIR" "$(dirname "$VERSION_FILE")"

# Stamp the version file BEFORE the loop so a partial run still
# carries something the firstboot reconcile can find (chart bytes
# may already match an earlier successful bake).
echo "$SPATIUMDDI_VERSION" > "$VERSION_FILE"
echo "→ SPATIUMDDI_VERSION = $SPATIUMDDI_VERSION (stamped at $VERSION_FILE)"

resolve_source_tag() {
    # ``BAKE_SOURCE=local``: use the operator's local :dev images.
    # Three naming conventions in the wild:
    #   * ``ghcr.io/spatiumddi/<short>:dev`` — ``make build-supervisor``
    #     dual-tags this form, and dev-iso flows that tag manually.
    #   * ``spatiumddi-<short>:dev`` — ``docker compose build`` uses
    #     the compose project name (``spatiumddi``) as the image
    #     prefix.
    #   * ``<short>:dev`` — direct ``docker build`` without prefix.
    # Try them in order of specificity; the first one that exists
    # locally is the source tag we save from. On total miss the
    # caller fails with a clear error.
    #
    # ``BAKE_SOURCE=ghcr``: pull from ghcr at the requested version
    # (CI release path). Always the fully-qualified canonical name.
    local repo="$1"
    local short
    short="$(basename "$repo")"
    case "$BAKE_SOURCE" in
        local)
            for candidate in \
                "${repo}:dev" \
                "spatiumddi-${short}:dev" \
                "${short}:dev"; do
                if docker image inspect "$candidate" >/dev/null 2>&1; then
                    echo "$candidate"
                    return 0
                fi
            done
            # Nothing matched — return the canonical name so the
            # error message points at the most-likely-correct tag.
            echo "${repo}:dev"
            ;;
        ghcr)
            echo "${repo}:${SPATIUMDDI_VERSION}"
            ;;
        *)
            echo "ERROR: unknown BAKE_SOURCE=${BAKE_SOURCE}" >&2
            exit 2
            ;;
    esac
}

for repo in "${IMAGES[@]}"; do
    short="$(basename "$repo")"
    source_tag="$(resolve_source_tag "$repo")"
    target_tag="${repo}:${SPATIUMDDI_VERSION}"
    out_tar="$IMAGES_DIR/${short}.tar.zst"

    if [ "$BAKE_SOURCE" = "ghcr" ]; then
        echo "→ Pulling $source_tag …"
        docker pull "$source_tag" >/dev/null
    elif ! docker image inspect "$source_tag" >/dev/null 2>&1; then
        echo "ERROR: no local image found for $repo. Tried:" >&2
        echo "         ${repo}:dev" >&2
        echo "         spatiumddi-${short}:dev" >&2
        echo "         ${short}:dev" >&2
        echo "       Run 'make build' (control plane :dev images)" >&2
        echo "       and 'make build-supervisor' (supervisor :dev) first." >&2
        exit 3
    fi

    # Retag so containerd registers the image under the chart's
    # expected ghcr name. ``docker save`` writes whatever RepoTags
    # the image has at save time; without this step, the archive
    # would carry ``<short>:dev`` (local) — chart references
    # ``ghcr.io/spatiumddi/<short>:${SPATIUMDDI_VERSION}`` and the
    # kubelet would say ``ErrImagePull``.
    docker tag "$source_tag" "$target_tag"

    echo "→ Baking $target_tag → $out_tar"
    # docker save | zstd → containerd-readable archive. Atomic via
    # .new sibling so a crash mid-bake doesn't ship a torn tarball.
    tmp="${out_tar}.new"
    docker save "$target_tag" | zstd -T0 -19 -o "$tmp"
    mv "$tmp" "$out_tar"

    size="$(du -h "$out_tar" | awk '{print $1}')"
    echo "  ✓ $size"
done

# Issue #183 Phase 8 — 3rd-party observability images. Pull + save
# at upstream tags; no retag, no SPATIUMDDI_VERSION involvement.
# Operator opts in via ``observability.kubeStateMetrics.enabled`` /
# ``observability.nodeExporter.enabled`` in the chart's values.yaml;
# the bake always ships them so the toggle works air-gap.
#
# Slugged output filenames so two images from the same registry
# path prefix don't collide. ``kube-state-metrics`` + ``node-exporter``
# are distinct enough that ``basename`` works.
for image in "${OBSERVABILITY_IMAGES[@]}" "${METALLB_IMAGES[@]}" "${CNPG_IMAGES[@]}"; do
    short="$(basename "${image%%:*}")"
    out_tar="$IMAGES_DIR/${short}.tar.zst"

    if ! docker image inspect "$image" >/dev/null 2>&1; then
        echo "→ Pulling $image …"
        docker pull "$image" >/dev/null
    fi

    echo "→ Baking $image → $out_tar"
    tmp="${out_tar}.new"
    docker save "$image" | zstd -T0 -19 -o "$tmp"
    mv "$tmp" "$out_tar"

    size="$(du -h "$out_tar" | awk '{print $1}')"
    echo "  ✓ $size"
done

TOTAL="$(du -hc "$IMAGES_DIR"/*.tar.zst 2>/dev/null | tail -1 | awk '{print $1}')"
# Prune stale image archives that this bake no longer produces (#277).
# bake-images.sh appends but never cleaned, so an image dropped from the
# lists above (e.g. the standalone ``postgres:16-alpine`` once the
# appliance went CNPG-only) left its ~100 MB ``.tar.zst`` behind to be
# baked into the ISO forever as dead weight. Compute the expected
# basenames from every list + the externally-fetched k3s airgap bundle
# (produced by ``appliance-fetch-k3s``, NOT this script — never prune
# it), then remove any other ``*.tar.zst``.
expected=()
for repo in "${IMAGES[@]}" "${OBSERVABILITY_IMAGES[@]}" "${METALLB_IMAGES[@]}" "${CNPG_IMAGES[@]}"; do
    expected+=("$(basename "${repo%%:*}").tar.zst")
done
expected+=("k3s-airgap-images-amd64.tar.zst")  # fetched separately — keep
for f in "$IMAGES_DIR"/*.tar.zst; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"
    keep=false
    for e in "${expected[@]}"; do
        [ "$base" = "$e" ] && { keep=true; break; }
    done
    if [ "$keep" = false ]; then
        echo "→ Pruning stale baked image (no longer in the bake list): $base"
        rm -f "$f"
    fi
done

TOTAL_COUNT=$((${#IMAGES[@]} + ${#OBSERVABILITY_IMAGES[@]} + ${#METALLB_IMAGES[@]} + ${#CNPG_IMAGES[@]}))
echo "✓ ${TOTAL_COUNT} images baked into $IMAGES_DIR ($TOTAL)"
echo "  k3s auto-imports these at startup (no firstboot shell-out required)"
