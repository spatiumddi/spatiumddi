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
#          (only apk today), or the finding is a language package (a
#          new PyPI/npm release is always installable). Availability is
#          unknown, so the outcome is what it was before this script
#          existed: refuse. Exit 1.
#
# A missing, empty or unparsable report is a hard failure: an image
# nothing scanned is never a pass.
#
# Requires jq and docker (the image must be present locally). If trivy is
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
# One `apk upgrade --simulate` against the live index yields, for every
# upgradable package, the version `apk upgrade` WOULD install — the exact
# thing the Dockerfile's `apk upgrade` line can reach. `--no-cache` fetches
# a fresh index; `--user 0` because runtime images drop privileges and
# apk needs root to resolve. Kept as "pkg<TAB>version" lines (no bash-4
# associative arrays, so this also runs under macOS's bash 3).
available=""
probe_ok=0
if [ "$os_family" = "alpine" ]; then
  if sim=$(docker run --rm --user 0 --entrypoint sh "$image" \
             -c 'apk upgrade --simulate --no-cache 2>/dev/null'); then
    probe_ok=1
    available=$(printf '%s\n' "$sim" \
      | sed -nE 's/^\([0-9]+\/[0-9]+\) Upgrading ([^ ]+) \(([^ ]+) -> ([^)]+)\)$/\1\t\3/p')
  else
    echo "::warning::could not run 'apk upgrade --simulate' in ${image}; availability unknown"
  fi
fi

# Version the index would install for $1, empty if nothing newer.
offered_for() {
  printf '%s\n' "$available" | awk -F '\t' -v p="$1" '$1 == p { print $2; exit }'
}

# apk's own comparator, so "-r1 vs -r0", "_p1" and friends are judged the
# way apk judges them. Prints one of < = > per line of "A B" on stdin.
apk_compare() {
  docker run --rm -i --user 0 --entrypoint sh "$image" -c '
    while read -r a b; do apk version -t "$a" "$b"; done'
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
  elif [ "$os_family" != "alpine" ]; then
    verdict=FAIL
    reason="cannot query a ${os_family:-unknown} package index; treating as installable"
  elif [ "$probe_ok" -ne 1 ]; then
    verdict=FAIL
    reason="package index probe failed; treating as installable"
  elif [ -z "$fixed" ]; then
    # Should not happen under --ignore-unfixed; if it does, the finding
    # is unfixed and the scan policy already says those do not block.
    verdict=DEFER
    reason="no fixed version known"
  elif offered=$(offered_for "$pkg") && [ -z "$offered" ]; then
    verdict=DEFER
    reason="the index offers nothing newer than ${installed} — fix announced, not yet published"
  else
    cmp=$(printf '%s %s\n' "$offered" "$fixed" | apk_compare) || cmp=""
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
        reason="could not compare ${offered} with ${fixed} (apk said '${cmp}'); treating as installable"
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
