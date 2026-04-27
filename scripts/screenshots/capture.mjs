// Headless screenshot driver for SpatiumDDI.
//
// Walks every route in ``routes.mjs``, waits for the page to settle,
// captures a fixed-viewport PNG, and drops it into
// ``docs/assets/screenshots/`` (overwrites in place — the README
// markdown doesn't change).
//
// Requires the dev stack to be running and reachable at the configured
// base URL (default ``http://localhost:8077``). Login uses admin / admin
// unless overridden — the seed data flow assumes the same defaults.
//
// Determinism levers:
//   * Fixed viewport (1520×900 default — matches the existing manual
//     screenshots' width).
//   * Fixed locale (``en-US``) + timezone (``UTC``) so date columns
//     don't drift between hosts.
//   * Animations disabled via injected CSS so React/Tailwind transitions
//     don't render half-applied.
//   * ``Date.now`` / ``new Date()`` frozen to the ``--freeze-clock``
//     timestamp (default 2026-04-26T12:00:00Z) so "5 minutes ago" labels
//     are byte-stable. Workaround for node-playwright 1.38 lacking
//     ``page.clock.install`` (added in upstream 1.45).
//
// Usage:
//   node scripts/screenshots/capture.mjs
//   node scripts/screenshots/capture.mjs --base-url http://localhost:8077
//   node scripts/screenshots/capture.mjs --user admin --password 'My$tr0ng!'
//   node scripts/screenshots/capture.mjs --only dashboard,ipam
//   node scripts/screenshots/capture.mjs --width 1920 --height 1080
//
// Run via ``make screenshots`` for the project-default invocation.

// Debian's ``node-playwright`` package installs into /usr/share/nodejs
// where Node's CommonJS resolver can find it but the native ESM
// resolver cannot. Use ``createRequire`` to load Playwright via the CJS
// path; this works whether playwright was apt-installed (Debian) or
// npm-installed (a node_modules tree adjacent to the script).
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { routes } from "./routes.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const REPO_ROOT = resolve(__dirname, "..", "..");

// ── CLI parsing ────────────────────────────────────────────────────────
function parseArgs(argv) {
  const args = {
    baseUrl: "http://localhost:8077",
    user: "admin",
    password: "admin",
    outDir: resolve(REPO_ROOT, "docs/assets/screenshots"),
    width: 1520,
    height: 900,
    only: null, // comma-separated list of route names
    freezeClock: "2026-04-26T12:00:00Z",
    headless: true,
    chromiumPath: process.env.PLAYWRIGHT_CHROMIUM_PATH || "/usr/bin/chromium",
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    if (a === "--base-url") args.baseUrl = next();
    else if (a === "--user") args.user = next();
    else if (a === "--password") args.password = next();
    else if (a === "--out-dir") args.outDir = resolve(next());
    else if (a === "--width") args.width = parseInt(next(), 10);
    else if (a === "--height") args.height = parseInt(next(), 10);
    else if (a === "--only") args.only = next().split(",").map(s => s.trim());
    else if (a === "--freeze-clock") args.freezeClock = next();
    else if (a === "--no-freeze-clock") args.freezeClock = null;
    else if (a === "--headed") args.headless = false;
    else if (a === "--help" || a === "-h") {
      console.log(
        "Usage: node scripts/screenshots/capture.mjs [options]\n" +
        "  --base-url URL          API + UI base (default http://localhost:8077)\n" +
        "  --user NAME             login username (default admin)\n" +
        "  --password VALUE        login password (default admin)\n" +
        "  --out-dir PATH          where PNGs land (default docs/assets/screenshots)\n" +
        "  --width N               viewport width  (default 1520)\n" +
        "  --height N              viewport height (default 900)\n" +
        "  --only A,B              capture only the named routes\n" +
        "  --freeze-clock ISO      freeze Date.now to this timestamp\n" +
        "  --no-freeze-clock       use real wall clock\n" +
        "  --headed                show the browser window (debugging)\n"
      );
      process.exit(0);
    } else {
      console.error(`unknown arg: ${a}`);
      process.exit(2);
    }
  }
  return args;
}

// ── Determinism: frozen-clock init script ──────────────────────────────
// Replaces global Date so any UI code that calls ``new Date()`` /
// ``Date.now()`` sees a fixed instant. Locale + timezone come from the
// browser context options below; this only handles the wall-clock value.
function freezeClockScript(iso) {
  const fixedMs = Date.parse(iso);
  return `(() => {
    const FIXED = ${fixedMs};
    const RealDate = Date;
    const Frozen = function Date(...args) {
      if (!new.target) return new RealDate(FIXED).toString();
      if (args.length === 0) return new RealDate(FIXED);
      return new RealDate(...args);
    };
    Frozen.now = () => FIXED;
    Frozen.parse = RealDate.parse;
    Frozen.UTC = RealDate.UTC;
    Frozen.prototype = RealDate.prototype;
    globalThis.Date = Frozen;
  })();`;
}

// CSS injected into every page that disables animations + transitions
// so screenshots aren't captured mid-fade. ``caret-color: transparent``
// keeps the focus caret out of input snapshots.
const NO_ANIM_CSS = `
  *, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
    caret-color: transparent !important;
    scroll-behavior: auto !important;
  }
`;

// ── Login flow ─────────────────────────────────────────────────────────
// The UI's login route renders at ``/login`` with two inputs and a
// submit button. We rely on field name="username" / name="password"
// (which is what the React component uses for ARIA naming + autofill).
// On force-password-change-on-first-login the API returns a 200 with
// ``force_password_change=true`` — the UI redirects to a change-pwd
// modal. This script doesn't auto-resolve that case; rotate the admin
// password (or seed a non-default password) before running.
async function login(page, baseUrl, user, password) {
  await page.goto(`${baseUrl}/login`, { waitUntil: "networkidle" });
  // SPA — inputs only exist after React renders. Wait explicitly.
  const usernameInput = page
    .locator("input[name='username'], input[type='text']")
    .first();
  const passwordInput = page
    .locator("input[name='password'], input[type='password']")
    .first();
  await usernameInput.waitFor({ timeout: 10_000 });
  await usernameInput.fill(user);
  await passwordInput.fill(password);
  try {
    await Promise.all([
      page.waitForURL((url) => !url.pathname.startsWith("/login"), {
        timeout: 15_000,
      }),
      page.locator("button[type='submit']").first().click(),
    ]);
  } catch (e) {
    // Most common cause: bad password (we're still on /login showing an
    // error toast) or force_password_change=true (we land on a change
    // password modal). Surface a hint instead of the raw timeout.
    const stillOnLogin = page.url().includes("/login");
    if (stillOnLogin) {
      throw new Error(
        `login as '${user}' failed — still on /login after submit. ` +
        "Wrong password, or force_password_change=true on the user " +
        "(see scripts/screenshots/README.md → Login flow)."
      );
    }
    throw e;
  }
}

// ── Per-route capture ──────────────────────────────────────────────────
async function captureRoute(page, baseUrl, route, outDir) {
  const target = `${baseUrl}${route.path}`;
  await page.goto(target, { waitUntil: "domcontentloaded" });
  if (route.waitFor) {
    try {
      await page.locator(route.waitFor).first().waitFor({ timeout: 10_000 });
    } catch {
      // Fall through — the screenshot will show whatever rendered.
      // Better than failing the whole run on one finicky page.
    }
  }
  // Let React Query settle + any post-mount refetches finish.
  await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
  // Belt + braces: a small final settle for chart libs that animate
  // even with our CSS override (Recharts uses inline transforms).
  await page.waitForTimeout(500);
  const out = resolve(outDir, `${route.name}.png`);
  await page.screenshot({ path: out, fullPage: false });
  return out;
}

// ── Stack reachability probe ───────────────────────────────────────────
// /health/live is mounted at the root (not under /api/v1) and returns
// {"status":"ok"} when the API process is alive.
async function probe(baseUrl) {
  const r = await fetch(`${baseUrl}/health/live`).catch(() => null);
  if (!r || !r.ok) {
    throw new Error(
      `dev stack not reachable at ${baseUrl}/health/live — ` +
      "start it with `make dev` (or `make up`) first."
    );
  }
}

// ── Main ───────────────────────────────────────────────────────────────
async function main() {
  const args = parseArgs(process.argv);

  if (!existsSync(args.chromiumPath)) {
    console.error(
      `chromium not found at ${args.chromiumPath} — install with: ` +
      `sudo apt-get install -y chromium`
    );
    process.exit(1);
  }

  await probe(args.baseUrl);
  await mkdir(args.outDir, { recursive: true });

  const targets = args.only
    ? routes.filter(r => args.only.includes(r.name))
    : routes.filter(r => !r.skip);

  if (targets.length === 0) {
    console.error("no routes selected (check --only filter)");
    process.exit(2);
  }

  console.log(`📷  ${targets.length} route(s) → ${args.outDir}`);
  console.log(`    base ${args.baseUrl}  ·  viewport ${args.width}×${args.height}`);

  const browser = await chromium.launch({
    executablePath: args.chromiumPath,
    headless: args.headless,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    const context = await browser.newContext({
      viewport: { width: args.width, height: args.height },
      locale: "en-US",
      timezoneId: "UTC",
      deviceScaleFactor: 1,
    });
    if (args.freezeClock) {
      await context.addInitScript(freezeClockScript(args.freezeClock));
    }

    const page = await context.newPage();
    // Reinject the no-animation CSS after every navigation. Playwright
    // 1.38 doesn't expose ``addStyleTag`` at the context level, and a
    // single page-level call doesn't survive ``page.goto``.
    page.on("load", () => {
      page.addStyleTag({ content: NO_ANIM_CSS }).catch(() => {});
    });

    console.log("🔐  login");
    await login(page, args.baseUrl, args.user, args.password);

    let ok = 0;
    let failed = 0;
    for (const route of targets) {
      try {
        const out = await captureRoute(page, args.baseUrl, route, args.outDir);
        console.log(`✓   ${route.name.padEnd(22)} ${out}`);
        ok++;
      } catch (e) {
        console.log(`✗   ${route.name.padEnd(22)} ${e.message}`);
        failed++;
      }
    }

    console.log(`\nfinished: ${ok} ok, ${failed} failed`);
    if (failed > 0) process.exit(1);
  } finally {
    await browser.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
