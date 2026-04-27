// List of routes captured by ``capture.mjs``. Add a new entry here to
// extend coverage; the file name (``<name>.png``) is what lands in
// ``docs/assets/screenshots/``.
//
// Fields:
//   name     — output file stem; matches the README's <img src=…>
//   path     — URL path (no host); navigated to after login
//   waitFor  — optional selector to wait for after navigation. Falls
//              back to ``networkidle`` + the global settle delay if
//              omitted. Loose selectors like ``h1`` or ``table`` are
//              fine — the goal is "page has rendered" not "specific
//              element exists".
//   skip     — optional reason string. Skipped routes log a "·" and
//              don't error.
//
// The first five entries below are the README-referenced screenshots
// and replace the existing files in-place when re-captured. Anything
// after that is documentation / blog material.

export const routes = [
  // ── README hero screenshots ────────────────────────────────────────
  { name: "dashboard", path: "/", waitFor: "h1, h2" },
  { name: "ipam", path: "/ipam", waitFor: "h1, h2" },
  { name: "dns", path: "/dns", waitFor: "h1, h2" },
  { name: "dhcp", path: "/dhcp", waitFor: "h1, h2" },
  { name: "vlans", path: "/vlans", waitFor: "h1, h2" },

  // ── Operator surfaces (post-2026.04.26) ────────────────────────────
  { name: "logs", path: "/logs", waitFor: "h1, h2" },
  { name: "trash", path: "/admin/trash", waitFor: "h1, h2" },
  { name: "platform-insights", path: "/admin/insights", waitFor: "h1, h2" },
  { name: "integrations", path: "/admin/integrations", waitFor: "h1, h2" },
];
