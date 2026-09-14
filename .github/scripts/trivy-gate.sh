#!/usr/bin/env bash
# trivy-gate.sh — turn a Trivy image report into a publish/refuse verdict.
#
#   usage: .github/scripts/trivy-gate.sh <trivy-report.json> <image-ref>
#
# The report is what `trivy image --format json` wrote for <image-ref>,
# scanned with `--severity HIGH,CRITICAL --ignore-unfixed` — so every
# finding in it is one Trivy believes has a fix. This script asks the
# one question Trivy cannot: is that fix INSTALLABLE from the package
# index the image builds against, right now?
#
# Trivy's "fixed" comes from the distro's security database. Alpine's
# secdb is updated when the fix is COMMITTED to aports; the built .apk
# reaches dl-cdn hours later (the builders and the CDN sync are separate
# steps). In that window a finding is "fixed" to Trivy and unfixable to
# `apk upgrade` — no Dockerfile change can clear it, and a gate that
# fails on it just blocks the nightly until the mirrors catch up.
# nightly-20260905 died exactly this way: util-linux 2.42.3 landed in
# aports at 00:08Z, the frontend image's fresh `apk upgrade` at 10:45Z
# picked up every other fix on the mirror but libuuid stayed at
# 2.42.1-r0 (7 HIGH), and the package appeared on the CDN at 13:53Z.
#
# Each finding is sorted into one of three outcomes:
#
#   FAIL   the index offers a version >= the fixed one. A rebuild would
#          cure this (stale layer cache, missing `apk upgrade`, a floor
#          that needs bumping) — the image must not ship. Exit 1.
#   DEFER  the index offers nothing newer, or something newer that is
#          still below the fixed version. Announced, not published.
#          Nothing a rebuild can do until the mirrors catch up, so it
#          is treated the way `--ignore-unfixed` already treats the
#          rest: reported (a workflow warning + step summary), not
#          blocking. The next build — whose package layer is rebuilt
#          against the current index — picks the fix up. Exit 0.
#   FAIL   the image's package manager is not one this script can ask
#          (apk and apt today), the probe itself failed, or the finding
#          is a language package (a new PyPI/npm release is always
#          installable). Availability is unknown, so the outcome is what
#          it was before this script existed: refuse. Exit 1.
#
# Debian/Ubuntu images are asked the same question a different way. apk's
# `--simulate` reports only what it WOULD upgrade, so a package missing
# from its output means "nothing newer exists"; `apt-cache policy` answers
# for every package it is asked about, so a package missing from ITS
# output means the index could not resolve the name at all — unknown
# availability, which is a FAIL, not a DEFER. Same question, opposite
# meaning for the same silence; getting that backwards would turn the
# fail-closed branch into a fail-open one.
#
# A missing, empty or unparsable report is a hard failure: an image
# nothing scanned is never a pass.
#
# Requires jq and docker (the image must be present locally, and the probe
# needs network access to the distro's mirrors). If trivy is
# on PATH the familiar table is printed first (`trivy convert`); otherwise
# a compact one is rendered from the JSON.
set -euo pipefail

usage() {
  echo "usage: $0 <trivy-report.json> <image-ref>" >&2
  exit 2
}
[ $# -eq 2 ] || usage
report=$1
image=$2

fail() {
  echo "::error::$*"
  exit 1
}

[ -s "$report" ] \
  || fail "trivy report '$report' is missing or empty — refusing to pass an unscanned image"
jq -e . "$report" >/dev/null 2>&1 \
  || fail "trivy report '$report' is not valid JSON"

# ── The report, for humans ──────────────────────────────────────────────
if command -v trivy >/dev/null 2>&1; then
  trivy convert --quiet --format table "$report" || true
else
  jq -r '
    .Results[]? as $r
    | ($r.Vulnerabilities // [])[]
    | "\($r.Target)\t\(.PkgName)\t\(.VulnerabilityID)\t\(.Severity)\t\(.InstalledVersion) -> \(.FixedVersion // "-")"
  ' "$report" | column -t -s $'\t' || true
fi
echo

# ── Findings: class, pkg, installed, fixed, id, severity ────────────────
# FixedVersion can list several versions ("1.2-r0, 1.3-r0"); the first
# is the lowest one that clears the CVE, which is the one to test for.
# An absent one is emitted as "-" — `read` collapses adjacent tabs, so an
# empty field would shift every column after it.
findings=$(jq -r '
  .Results[]? as $r
  | ($r.Vulnerabilities // [])[]
  | [ $r.Class, .PkgName, .InstalledVersion,
      ((.FixedVersion // "") | split(",") | (.[0] // "") | gsub("^\\s+|\\s+$"; "")
        | if . == "" then "-" else . end),
      .VulnerabilityID, .Severity ]
  | @tsv' "$report")

os_family=$(jq -r '.Metadata.OS.Family // ""' "$report")
os_name=$(jq -r '.Metadata.OS.Name // ""' "$report")

echo "── trivy-gate: ${image} (${os_family:-unknown os} ${os_name})"
if [ -z "$findings" ]; then
  echo "✓ no HIGH/CRITICAL findings with a fix"
  exit 0
fi

# ── What can the image's own package index install today? ──────────────
# Kept as "pkg<TAB>version" lines (no bash-4 associative arrays, so this
# also runs under macOS's bash 3). `--user 0` throughout because runtime
# images drop privileges and neither package manager resolves as non-root.
available=""
probe_ok=0

case "$os_family" in
  alpine)
    # One `apk upgrade --simulate` against the live index yields, for
    # every upgradable package, the version `apk upgrade` WOULD install —
    # the exact thing the Dockerfile's `apk upgrade` line can reach.
    # `--no-cache` fetches a fresh index.
    if sim=$(docker run --rm --user 0 --entrypoint sh "$image" \
               -c 'apk upgrade --simulate --no-cache 2>/dev/null'); then
      probe_ok=1
      available=$(printf '%s\n' "$sim" \
        | sed -nE 's/^\([0-9]+\/[0-9]+\) Upgrading ([^ ]+) \(([^ ]+) -> ([^)]+)\)$/\1\t\3/p')
    else
      echo "::warning::could not run 'apk upgrade --simulate' in ${image}; availability unknown"
    fi
    ;;
  debian | ubuntu)
    # `apt-cache policy` reports the CANDIDATE — the version `apt-get
    # install`/`apt-get upgrade` would install — for each named package.
    # Deliberately not `apt-get -s upgrade`: plain upgrade holds back any
    # fix that needs a new dependency, and a held-back fix reported as
    # "not on the mirrors yet" would DEFER forever on a finding a
    # dist-upgrade or an explicit install could cure. The candidate is
    # the strict reading, which is the direction this gate errs in.
    #
    # Only the packages actually flagged are asked about, so the probe
    # stays one docker run regardless of image size. Shipped Dockerfiles
    # end with `rm -rf /var/lib/apt/lists/*`, hence the `apt-get update`.
    #
    # Names are filtered to Debian's own policy character class before
    # being interpolated into the shell command. A name outside it is not
    # sanitised, it is DROPPED — so it is then absent from `available`
    # and the loop below fails it as unverifiable, which is the direction
    # that cannot publish something on the strength of an unparsed name.
    pkgs=$(printf '%s\n' "$findings" \
      | awk -F '\t' '$1 == "os-pkgs" && $2 ~ /^[a-z0-9][a-z0-9+.-]*$/ { print $2 }' \
      | sort -u | tr '\n' ' ')
    if [ -z "$pkgs" ]; then
      # Nothing to ask about: every finding is a language package, which
      # the verdict loop answers without the index. Probing anyway would
      # emit a warning naming apt as the problem when apt is not involved.
      probe_ok=1
    elif pol=$(docker run --rm --user 0 --entrypoint sh "$image" -c "
           apt-get update -qq >/dev/null 2>&1 || exit 1
           apt-cache policy ${pkgs} 2>/dev/null"); then
      probe_ok=1
      # "pkgname:" at column 0, then an indented "Candidate: <version>".
      # A package apt cannot resolve produces neither, so it is absent
      # from `available` and the loop below fails it as unverifiable.
      available=$(printf '%s\n' "$pol" | awk '
        /^[^[:space:]]/          { pkg = $1; sub(/:$/, "", pkg); next }
        /^[[:space:]]+Candidate:/ { if (pkg != "" && $2 != "(none)") print pkg "\t" $2; pkg = "" }')
    else
      echo "::warning::could not query the apt index in ${image}; availability unknown"
    fi
    ;;
  *)
    echo "::warning::no package-index probe for '${os_family:-unknown}'; availability unknown"
    ;;
esac

# Version the index would install for $1, empty if it offered nothing.
offered_for() {
  printf '%s\n' "$available" | awk -F '\t' -v p="$1" '$1 == p { print $2; exit }'
}

# The distro's OWN comparator, so "-r1 vs -r0", "+deb13u2", "~deb13u1",
# "_p1" and friends are judged the way the package manager judges them.
# Prints one of < = > per line of "A B" on stdin.
version_compare() {
  case "$os_family" in
    alpine)
      docker run --rm -i --user 0 --entrypoint sh "$image" -c '
        while read -r a b; do apk version -t "$a" "$b"; done'
      ;;
    debian | ubuntu)
      docker run --rm -i --user 0 --entrypoint sh "$image" -c '
        while read -r a b; do
          if dpkg --compare-versions "$a" gt "$b"; then echo ">"
          elif dpkg --compare-versions "$a" eq "$b"; then echo "="
          else echo "<"; fi
        done'
      ;;
  esac
}

fails=0
defers=0
summary=()
while IFS=$'\t' read -r class pkg installed fixed id sev; do
  [ -n "$pkg" ] || continue
  [ "$fixed" != "-" ] || fixed=""
  where="${pkg} ${id} (${sev}): installed ${installed}, fixed ${fixed:-?}"
  verdict=""
  reason=""
  if [ "$class" != "os-pkgs" ]; then
    verdict=FAIL
    reason="language package — a newer release is always installable; update the pin"
  elif [ "$probe_ok" -ne 1 ]; then
    verdict=FAIL
    reason="could not query the ${os_family:-unknown} package index; treating as installable"
  elif [ -z "$fixed" ]; then
    # Should not happen under --ignore-unfixed; if it does, the finding
    # is unfixed and the scan policy already says those do not block.
    verdict=DEFER
    reason="no fixed version known"
  elif offered=$(offered_for "$pkg") && [ -z "$offered" ]; then
    # Silence means different things to the two probes — see the header.
    if [ "$os_family" = "alpine" ]; then
      verdict=DEFER
      reason="the index offers nothing newer than ${installed} — fix announced, not yet published"
    else
      verdict=FAIL
      reason="the index does not list ${pkg}; treating as installable"
    fi
  else
    cmp=$(printf '%s %s\n' "$offered" "$fixed" | version_compare) || cmp=""
    case "$cmp" in
      '>' | '=')
        verdict=FAIL
        reason="the index offers ${offered} — rebuild the package layer"
        ;;
      '<')
        verdict=DEFER
        reason="the index offers ${offered}, still below the fix — announced, not yet published"
        ;;
      *)
        verdict=FAIL
        reason="could not compare ${offered} with ${fixed} (got '${cmp}'); treating as installable"
        ;;
    esac
  fi

  case "$verdict" in
    FAIL)
      fails=$((fails + 1))
      echo "✗ FAIL  ${where} — ${reason}"
      echo "::error title=${image} ${id}::${where} — ${reason}"
      ;;
    DEFER)
      defers=$((defers + 1))
      echo "⏳ DEFER ${where} — ${reason}"
      echo "::warning title=${image} ${id} deferred::${where} — ${reason}"
      ;;
  esac
  summary+=("| ${verdict} | \`${pkg}\` | ${id} | ${sev} | ${installed} | ${fixed:-?} | ${reason} |")
done <<< "$findings"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Trivy gate — \`${image}\`"
    echo
    echo "| Verdict | Package | CVE | Severity | Installed | Fixed | Why |"
    echo "|---|---|---|---|---|---|---|"
    printf '%s\n' "${summary[@]}"
    echo
    echo "FAIL blocks the push. DEFER = the fix is in the distro's security database but not yet on the package mirrors; it is reported like an unfixed finding and picked up by the next build."
  } >> "$GITHUB_STEP_SUMMARY"
fi

echo
if [ "$fails" -gt 0 ]; then
  echo "✗ ${fails} finding(s) have an installable fix — refusing to publish ${image}"
  exit 1
fi
echo "✓ ${image}: ${defers} finding(s) deferred (fix announced, not yet on the mirrors); nothing a rebuild could cure"
exit 0
