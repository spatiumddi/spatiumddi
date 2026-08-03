#!/usr/bin/env bash
#
# Does a change set require the backend test suite? (issue #813)
#
# Reads changed paths on stdin, one per line. Prints "true" if the suite must
# run, "false" if every changed path is known not to affect it.
#
# ── The list below is a DENY-list, and that is the whole design ──────────────
#
# An allow-list ("run when backend/** changes") fails open in the wrong
# direction: a new top-level directory nobody remembered to add silently stops
# running the tests, and the failure is invisible — a green PR that was never
# tested. A deny-list fails the other way. Anything unrecognised runs the
# suite, so the worst case of forgetting to update this file is a few wasted
# runner-minutes.
#
# So: only add a path here when you can say why the backend suite cannot
# possibly care about it.
#
# ── Why appliance/ and agent/ are NOT here ───────────────────────────────────
#
# They look like other people's code, but two backend tests load files out of
# them directly:
#
#   backend/tests/test_spatium_console.py
#       → appliance/mkosi.extra/usr/local/bin/spatium-console  (52 tests)
#   backend/tests/test_appliance_firewall_render.py
#       → agent/supervisor/spatium_supervisor/firewall_renderer.py
#
# Skipping the suite on an appliance-only PR would have dropped every one of
# those. Check for new cross-boundary reads before adding either.
#
# ── Testability ──────────────────────────────────────────────────────────────
#
# Path matching lives here rather than inline in ci.yml so it can be exercised
# without pushing a branch: backend/tests/test_ci_backend_relevant.sh.py pipes
# synthetic change sets through it. The workflow computes the diff and pipes
# it in; this script never shells out to git.

set -euo pipefail

# Paths that cannot affect the backend test suite.
#
#   docs/       Jekyll site + specs. No backend test reads them; the
#               docs-diagrams job covers the SVG geometry separately.
#   website/    Marketing site source, not built or imported by anything here.
#   frontend/   Separate toolchain, covered by the frontend jobs.
#   charts/     Helm templates. test_upgrades_chart_bump.py exercises the
#               chart-bump LOGIC against inline YAML, never the real chart.
#   k8s/        Static manifests, read by nothing in the suite.
#   *.md        Root-level prose (README, CHANGELOG, CLAUDE.md, ...). Nested
#               *.md are covered by their own directory's entry, or fall
#               through and run the suite — which is the safe direction.
#   NOTICE      Third-party attribution.
#   LICENSE     Apache 2.0 text.
IRRELEVANT='^(docs/|website/|frontend/|charts/|k8s/|[^/]+\.md$|NOTICE$|LICENSE$)'

saw_any=0
while IFS= read -r path; do
    [ -n "$path" ] || continue
    saw_any=1
    if ! printf '%s\n' "$path" | grep -qE "$IRRELEVANT"; then
        # One relevant file is enough — no need to read the rest.
        echo "true"
        exit 0
    fi
done

# An empty change set is not evidence that nothing needs testing; it means the
# diff failed, the caller piped nothing, or something else went wrong upstream.
# Run the suite.
if [ "$saw_any" -eq 0 ]; then
    echo "true"
    exit 0
fi

echo "false"
