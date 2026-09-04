#!/usr/bin/env bash
#
# Lint, render and schema-check both Helm charts (#966).
#
# Until this existed nothing on a PR parsed the charts at all: the one
# PR-time helm job (agent-e2e) is path-filtered to charts/spatiumddi/** and
# installs only the umbrella chart, so charts/spatiumddi-appliance/ was first
# read by helm during ``release.yml``'s ``helm package`` — a template that
# failed to render broke the release, not the PR that introduced it. And no
# job ran ``helm lint`` for either chart.
#
# Three gates per render, each catching what the previous cannot:
#
#   helm lint        — template syntax, values schema, chart metadata.
#   helm template    — the render itself, with the role / feature toggles
#                      flipped ON, because a template that only renders at
#                      defaults has not been rendered (every appliance role
#                      is ``enabled: false`` by default).
#   kubeconform      — the rendered objects against the Kubernetes API
#                      schemas, -strict so an unknown field (a key indented
#                      under the wrong parent, the classic helm mistake) is
#                      an error rather than something the apiserver would
#                      silently drop. CRDs (the CNPG ``Cluster``) resolve
#                      through the datreeio CRDs-catalog.
#   no-besteffort    — every render: each serving container must carry a
#                      CPU + memory request or limit (#965). A ``with`` guard
#                      that tests the wrong values path renders no
#                      ``resources:`` block and passes the three gates above.
#   toggle-coverage  — every ``.Values.x.enabled`` / ``.kind`` a template is
#                      gated on must be flipped by at least one render, so a
#                      new gate cannot silently fall out of the matrix.
#
# Runs anywhere helm + kubeconform + python3 (with PyYAML) are on PATH; the
# CI job and ``make charts-lint`` both call it. Rendered manifests are left
# in $OUT for inspection.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${OUT:-$(mktemp -d)}"
K8S_VERSION="${K8S_VERSION:-1.31.0}"
# Group/kind/version-templated so any CRD in the catalog resolves; the one
# we render today is postgresql.cnpg.io/Cluster.
CRD_SCHEMAS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

UMBRELLA="$ROOT/charts/spatiumddi"
APPLIANCE="$ROOT/charts/spatiumddi-appliance"

for tool in helm kubeconform python3; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

failures=0

lint() { # chart [helm --set args...]
    local chart="$1"; shift
    echo "── helm lint $(basename "$chart") $*"
    helm lint --strict "$chart" "$@" || failures=$((failures + 1))
}

# kubeconform caches fetched schemas only in memory, per invocation; six
# invocations would fetch every kind's schema six times from GitHub raw. A
# disk cache makes renders 2-6 free, and CI persists it across runs.
KUBECONFORM_CACHE="${KUBECONFORM_CACHE:-$OUT/.schema-cache}"
mkdir -p "$KUBECONFORM_CACHE"

render() { # name chart [helm --set args...]
    local name="$1" chart="$2"; shift 2
    local file="$OUT/$name.yaml"
    echo "── helm template $name ($(basename "$chart")) $*"
    if ! helm template "$name" "$chart" --kube-version "$K8S_VERSION" "$@" > "$file"; then
        failures=$((failures + 1)); return
    fi
    echo "   $(grep -c '^kind:' "$file") objects → $file"
    # CustomResourceDefinition is skipped: the only CRDs rendered are the
    # CNPG operator's, vendored from upstream via the subchart — not ours to
    # validate, and the upstream schema set carries no strict variant for
    # the kind. Every CR *instance* (the ``Cluster``) is still checked.
    kubeconform -strict -summary \
        -kubernetes-version "$K8S_VERSION" \
        -schema-location default \
        -schema-location "$CRD_SCHEMAS" \
        -skip CustomResourceDefinition \
        -cache "$KUBECONFORM_CACHE" \
        "$file" || failures=$((failures + 1))
    python3 "$ROOT/.github/scripts/chart-no-besteffort.py" "$file" || failures=$((failures + 1))
}

coverage() { # chart [every --set arg from every render of that chart...]
    local chart="$1"; shift
    echo "── toggle coverage $(basename "$chart")"
    python3 "$ROOT/.github/scripts/chart-toggle-coverage.py" "$chart" "$@" || failures=$((failures + 1))
}

# Subcharts. The umbrella has none today; the appliance vendors CNPG (#272)
# and this is the first time the dependency resolves before release.
helm dependency update "$UMBRELLA"
helm dependency update "$APPLIANCE"

# ── Umbrella chart ──────────────────────────────────────────────────────────
# Every template gate on: the agents, ingress, the slot-image mirror, HPA, the
# three RBAC toggles + service control + appliance host mounts, frontend TLS,
# Redis auth. ``chart-toggle-coverage.py`` fails this script if a template
# grows a gate that no set below flips.
UMBRELLA_ALL_ON=(
    --set dnsAgents.enabled=true
    --set dhcpAgents.enabled=true
    --set ingress.enabled=true
    --set slotImageMirror.enabled=true
    --set api.autoscaling.enabled=true
    --set api.serviceAccount.enabled=true
    --set api.serviceControl.enabled=true
    --set api.serviceControlRBAC.enabled=true
    --set api.upgradeOrchestratorRBAC.enabled=true
    --set api.applianceHostMounts.enabled=true
    --set frontend.tls.enabled=true
    --set redis.auth.enabled=true
)
# The HA topology docs/deployment/KUBERNETES.md points operators at: a
# CloudNativePG ``Cluster`` CR (the one CRD instance either chart renders —
# the datreeio schema location exists for it) + Redis Sentinel, with CNPG
# backups on.
UMBRELLA_HA=(
    --set postgresql.kind=cnpg
    --set postgresql.cnpg.backup.enabled=true
    --set redis.kind=sentinel
)
UMBRELLA_EXTERNAL=(
    --set postgresql.enabled=false --set externalDatabase.host=pg.example
    --set redis.enabled=false --set externalRedis.host=redis.example
)
lint "$UMBRELLA"
lint "$UMBRELLA" "${UMBRELLA_ALL_ON[@]}"
lint "$UMBRELLA" "${UMBRELLA_HA[@]}"
render umbrella-defaults "$UMBRELLA"
render umbrella-all-on "$UMBRELLA" "${UMBRELLA_ALL_ON[@]}"
render umbrella-ha "$UMBRELLA" "${UMBRELLA_HA[@]}"
# Bring-your-own database + Redis: the shape k8s/ha/ installs use.
render umbrella-external-db "$UMBRELLA" "${UMBRELLA_EXTERNAL[@]}"
coverage "$UMBRELLA" "${UMBRELLA_ALL_ON[@]}" "${UMBRELLA_HA[@]}" "${UMBRELLA_EXTERNAL[@]}"

# ── Appliance chart ─────────────────────────────────────────────────────────
# Every role + every feature on at once. This is not a valid appliance (one
# node never runs all three DNS drivers) — it is the render that exercises
# every template, which is what matters here.
APPLIANCE_ALL_ON=(
    --set dnsBind9.enabled=true
    --set dnsPowerdns.enabled=true
    --set dnsTechnitium.enabled=true
    --set dhcpKea.enabled=true
    --set dhcpKea.relayVIP=10.0.0.5
    --set lookingGlass.enabled=true
    --set supervisor.enabled=true
    --set observability.kubeStateMetrics.enabled=true
    --set observability.nodeExporter.enabled=true
    --set dns.useMetalLBVIP=true
    --set cnpg.enabled=true
)
lint "$APPLIANCE"
lint "$APPLIANCE" "${APPLIANCE_ALL_ON[@]}"
render appliance-defaults "$APPLIANCE"
render appliance-all-on "$APPLIANCE" "${APPLIANCE_ALL_ON[@]}"
# The single-node default install shape: one DNS driver + DHCP + supervisor.
render appliance-full-stack "$APPLIANCE" \
    --set dnsBind9.enabled=true --set dhcpKea.enabled=true --set supervisor.enabled=true
coverage "$APPLIANCE" "${APPLIANCE_ALL_ON[@]}"

if [ "$failures" -ne 0 ]; then
    echo "charts: $failures gate(s) failed" >&2
    exit 1
fi
echo "charts: all gates passed (renders in $OUT)"
