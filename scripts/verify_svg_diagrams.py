#!/usr/bin/env python3
"""Verify documentation SVG diagrams render cleanly.

ASCII box-drawing diagrams degrade badly in a published docs site, so
``docs/assets/`` holds hand-authored SVG instead. Hand-authored SVG has
its own failure mode: text is positioned by absolute coordinate, so a
label that grew by one word silently spills out of its box or off the
canvas. Nothing in a diff shows that — it only appears in the rendered
page, which is exactly where readers see it.

This script measures the real thing. It loads each SVG in headless
Chromium and reads ``getBBox()`` per text node, so the numbers come
from the same font stack and shaping engine a browser uses, not from an
estimate. Five checks run against those measurements:

  overflow    text wider than the ``<rect>`` it is centred in
  clipped     any element extending past the viewBox
  collision   two text nodes on the same line whose boxes intersect
  tiny        font-size below the legibility floor
  malformed   unparseable SVG, or a text node the renderer dropped

Usage::

    python3 scripts/verify_svg_diagrams.py                # all diagrams
    python3 scripts/verify_svg_diagrams.py path/to/a.svg  # just one
    python3 scripts/verify_svg_diagrams.py --json         # machine-readable

Exits non-zero if any diagram fails, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAGRAM_ROOT = REPO_ROOT / "docs" / "assets"

# Logos are artwork, not diagrams — they legitimately use tight custom
# geometry and their own type treatment, so the box/legibility rules
# below don't apply to them.
EXCLUDE_NAMES = {"logo.svg", "logo-icon.svg"}

# Smallest font-size we consider legible in a published diagram. The
# house style bottoms out at 10px for annotation text; anything under
# that is a mistake rather than a deliberate choice.
MIN_FONT_SIZE = 9.5

# Slack allowed when comparing a text box against its container, in user
# units. Sub-pixel shaping differences between platforms are real, and a
# hairline overhang is invisible; a genuine overflow is several px.
EDGE_TOLERANCE = 1.0

# The measuring script. Runs inside the page, walks every text node, and
# reports geometry plus the tightest enclosing rect. Chromium's --dump-dom
# gives us the post-script DOM, so results come back in a <pre> we can parse.
#
# Every diagram is measured in ONE page load. Launching a browser per file
# meant ~30 cold starts, which is slow locally and times out on a CI runner
# whose Chromium starts far slower than a developer's. Batching is safe here
# because the only cross-document hazard would be duplicate element ids, and
# these diagrams carry none and use no <use> references — and text metrics
# depend on font and content, not on which gradient a fill resolved to.
MEASURE_JS = r"""
(function () {
  var all = [];
  document.querySelectorAll('svg[data-diagram]').forEach(function (svg) {
    all.push(measureOne(svg, +svg.getAttribute('data-diagram')));
  });
  return all;

  function measureOne(svg, idx) {
  var out = { idx: idx, texts: [], rects: [], root: null, error: null };
  try {
    var vb = svg.viewBox.baseVal;
    out.root = { x: vb.x, y: vb.y, w: vb.width, h: vb.height };

    // Collect candidate container rects once. A text label is judged
    // against the smallest rect that contains its anchor point, which
    // is how a human reads the diagram: the innermost box wins.
    var rects = Array.prototype.slice.call(svg.querySelectorAll('rect'));
    rects.forEach(function (r) {
      var bb = r.getBBox();
      out.rects.push({ x: bb.x, y: bb.y, w: bb.width, h: bb.height });
    });

    var texts = Array.prototype.slice.call(svg.querySelectorAll('text'));
    texts.forEach(function (t, i) {
      var bb, len;
      try { bb = t.getBBox(); len = t.getComputedTextLength(); }
      catch (e) { out.texts.push({ i: i, dropped: true, content: t.textContent }); return; }

      var cs = window.getComputedStyle(t);
      var fs = parseFloat(cs.fontSize) || 0;

      // Tightest containing rect by area, tested against the text's own
      // box rather than its anchor so wide centred labels are caught.
      var cx = bb.x + bb.width / 2, cy = bb.y + bb.height / 2;
      var best = null;
      out.rects.forEach(function (r) {
        if (cx >= r.x && cx <= r.x + r.w && cy >= r.y && cy <= r.y + r.h) {
          if (!best || r.w * r.h < best.w * best.h) best = r;
        }
      });

      out.texts.push({
        i: i,
        content: t.textContent,
        x: bb.x, y: bb.y, w: bb.width, h: bb.height,
        len: len,
        fontSize: fs,
        box: best
      });
    });
  } catch (e) {
    out.error = String(e && e.message || e);
  }
  return out;
  }
})()
"""

HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:#fff}}</style>
</head><body>
{svg}
<pre id="__result"></pre>
<script>
try {{
  document.getElementById('__result').textContent =
    JSON.stringify({measure});
}} catch (e) {{
  document.getElementById('__result').textContent =
    JSON.stringify({{ error: String(e) }});
}}
</script>
</body></html>
"""


@dataclass
class Finding:
    kind: str
    detail: str


@dataclass
class Result:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    text_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.findings


def _chromium() -> str:
    # google-chrome first: it is preinstalled on GitHub's ubuntu runners and
    # starts noticeably faster there than a Chromium pulled from apt, which
    # may be a snap wrapper. Falls back to whatever a developer has locally.
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    print(
        "error: no Chromium binary found — install chromium to run the "
        "geometry checks (apt-get install -y chromium).",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _inline(svg_path: Path, idx: int) -> str:
    """Prepare one SVG for inlining into the measurement page."""
    svg = svg_path.read_text(encoding="utf-8")
    # Strip the XML prolog — it is invalid partway through an HTML document
    # and Chromium's parser will bail on the rest of the file.
    if svg.lstrip().startswith("<?xml"):
        svg = svg.split("?>", 1)[1]
    # Tag the root element so the measuring script can attribute results back
    # to the right file. Only the first <svg> is the root; nested ones (there
    # are none today) would be measured as part of their parent.
    return svg.replace("<svg", f'<svg data-diagram="{idx}"', 1)


def measure_all(paths: list[Path], browser: str) -> dict[int, dict]:
    """Render every SVG in a single headless page and return measurements
    keyed by index into ``paths``.

    One launch rather than one-per-file: browser startup dominates the
    runtime, and a CI runner's Chromium is slow enough that per-file launches
    blow past any sane timeout.
    """
    parts, failed = [], {}
    for i, p in enumerate(paths):
        try:
            parts.append(_inline(p, i))
        except OSError as exc:
            failed[i] = {"error": f"unreadable: {exc}"}

    html = HTML_TEMPLATE.format(svg="\n".join(parts), measure=MEASURE_JS)

    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    browser,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    f"--user-data-dir={tmp}/profile",
                    "--virtual-time-budget=8000",
                    "--dump-dom",
                    page.as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return {i: {"error": "renderer timed out"} for i in range(len(paths))}

    dom = proc.stdout
    marker = '<pre id="__result">'
    start = dom.find(marker)
    if start == -1:
        return {
            i: {"error": "renderer produced no measurement block"}
            for i in range(len(paths))
        }
    start += len(marker)
    raw = dom[start : dom.find("</pre>", start)]
    # The DOM dump HTML-escapes the JSON payload.
    raw = (
        raw.replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {
            i: {"error": f"unparseable measurement payload: {exc}"}
            for i in range(len(paths))
        }

    out = dict(failed)
    for entry in payload:
        out[entry["idx"]] = entry
    for i in range(len(paths)):
        out.setdefault(i, {"error": "diagram did not render"})
    return out


def check(svg_path: Path, data: dict) -> Result:
    result = Result(path=svg_path)

    if data.get("error"):
        result.findings.append(Finding("malformed", data["error"]))
        return result

    root = data.get("root")
    if not root or not root.get("w"):
        result.findings.append(Finding("malformed", "missing or zero-size viewBox"))
        return result

    texts = [t for t in data.get("texts", []) if t.get("content", "").strip()]
    result.text_count = len(texts)

    for t in texts:
        label = t.get("content", "").strip()
        short = label if len(label) <= 48 else label[:45] + "..."

        if t.get("dropped"):
            result.findings.append(
                Finding("malformed", f"renderer dropped text {short!r}")
            )
            continue

        # 1. Clipped by the canvas.
        if (
            t["x"] < root["x"] - EDGE_TOLERANCE
            or t["y"] < root["y"] - EDGE_TOLERANCE
            or t["x"] + t["w"] > root["x"] + root["w"] + EDGE_TOLERANCE
            or t["y"] + t["h"] > root["y"] + root["h"] + EDGE_TOLERANCE
        ):
            result.findings.append(
                Finding(
                    "clipped",
                    f"{short!r} at x={t['x']:.0f},y={t['y']:.0f} "
                    f"w={t['w']:.0f} extends past viewBox "
                    f"{root['w']:.0f}x{root['h']:.0f}",
                )
            )

        # 2. Overflowing its own container box.
        box = t.get("box")
        if box:
            if (
                t["x"] < box["x"] - EDGE_TOLERANCE
                or t["x"] + t["w"] > box["x"] + box["w"] + EDGE_TOLERANCE
            ):
                overhang = max(
                    box["x"] - t["x"],
                    (t["x"] + t["w"]) - (box["x"] + box["w"]),
                )
                result.findings.append(
                    Finding(
                        "overflow",
                        f"{short!r} is {t['w']:.0f}px wide in a "
                        f"{box['w']:.0f}px box — spills {overhang:.0f}px",
                    )
                )

        # 3. Illegibly small.
        if 0 < t["fontSize"] < MIN_FONT_SIZE:
            result.findings.append(
                Finding("tiny", f"{short!r} at font-size {t['fontSize']:.1f}px")
            )

    # 4. Overlapping labels. Only compare pairs that share vertical space —
    #    diagrams legitimately stack many labels at the same x.
    for i, a in enumerate(texts):
        if a.get("dropped"):
            continue
        for b in texts[i + 1 :]:
            if b.get("dropped"):
                continue
            v_overlap = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            h_overlap = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            # Require a substantial intersection in both axes: glyph boxes
            # routinely share a pixel or two of leading without colliding.
            if v_overlap > 2.0 and h_overlap > 2.0:
                result.findings.append(
                    Finding(
                        "collision",
                        f"{a['content'].strip()[:28]!r} overlaps "
                        f"{b['content'].strip()[:28]!r} "
                        f"({h_overlap:.0f}x{v_overlap:.0f}px)",
                    )
                )

    return result


def collect(targets: list[str]) -> list[Path]:
    if targets:
        paths: list[Path] = []
        for t in targets:
            p = Path(t)
            if not p.is_absolute():
                p = (REPO_ROOT / p).resolve() if not p.exists() else p.resolve()
            paths.append(p)
        return paths
    return sorted(p for p in DIAGRAM_ROOT.rglob("*.svg") if p.name not in EXCLUDE_NAMES)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("svg", nargs="*", help="specific SVG files (default: all diagrams)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = ap.parse_args()

    browser = _chromium()
    paths = collect(args.svg)
    if not paths:
        print("no SVG diagrams found", file=sys.stderr)
        return 1

    measurements = measure_all(paths, browser)
    results = [check(p, measurements[i]) for i, p in enumerate(paths)]
    failed = [r for r in results if not r.ok]

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "path": str(r.path.relative_to(REPO_ROOT)),
                        "ok": r.ok,
                        "texts": r.text_count,
                        "findings": [
                            {"kind": f.kind, "detail": f.detail} for f in r.findings
                        ],
                    }
                    for r in results
                ],
                indent=2,
            )
        )
        return 1 if failed else 0

    for r in results:
        rel = (
            r.path.relative_to(REPO_ROOT)
            if r.path.is_relative_to(REPO_ROOT)
            else r.path
        )
        if r.ok:
            print(f"  ok    {rel}  ({r.text_count} labels)")
        else:
            print(f"  FAIL  {rel}")
            for f in r.findings:
                print(f"          [{f.kind}] {f.detail}")

    print()
    if failed:
        total = sum(len(r.findings) for r in failed)
        print(f"{len(failed)} of {len(results)} diagrams failed ({total} findings)")
        return 1
    print(f"all {len(results)} diagrams clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
