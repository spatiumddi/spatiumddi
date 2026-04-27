# Screenshot capture

Headless-browser screenshot pipeline for the README and the docs site.
Re-captures every route in [`routes.mjs`](routes.mjs) at a fixed
viewport with deterministic clock + locale, and overwrites the PNGs in
`docs/assets/screenshots/` so the README markdown doesn't need to
change.

## One-time install

Two layers:

```bash
# System: the browser binary (apt — Debian's chromium works fine)
sudo apt-get install -y chromium

# Project: playwright itself (npm — Debian's node-playwright 1.38 has
# an internal rimraf-API mismatch with Node 20 and is unusable here)
cd scripts/screenshots && npm install
```

`make screenshots` auto-runs `npm install` if the `node_modules`
directory is missing, so day-to-day you only need the apt step. The
`PLAYWRIGHT_CHROMIUM_PATH` env var (defaults to `/usr/bin/chromium`)
points playwright at the system browser, so playwright skips its own
browser-download step.

## Run

The dev stack must be running and reachable. From the repo root:

```bash
make screenshots
```

Or call the script directly with options:

```bash
node scripts/screenshots/capture.mjs --base-url http://localhost:8077
node scripts/screenshots/capture.mjs --only dashboard,ipam
node scripts/screenshots/capture.mjs --width 1920 --height 1080
node scripts/screenshots/capture.mjs --headed       # show the browser
node scripts/screenshots/capture.mjs --help
```

The script logs `✓` or `✗` per route and exits non-zero if any route
failed (so it slots into CI naturally).

## What it captures

| File                                | Route                  |
|-------------------------------------|------------------------|
| `dashboard.png`                     | `/`                    |
| `ipam.png`                          | `/ipam`                |
| `dns.png`                           | `/dns`                 |
| `dhcp.png`                          | `/dhcp`                |
| `vlans.png`                         | `/vlans`               |
| `logs.png`                          | `/logs`                |
| `trash.png`                         | `/admin/trash`         |
| `platform-insights.png`             | `/admin/insights`      |
| `integrations.png`                  | `/admin/integrations`  |

Add a new entry to [`routes.mjs`](routes.mjs) to extend coverage. The
`name` field becomes the output file stem.

## Determinism levers

These exist so re-running the script produces byte-identical PNGs (or
near-enough that visual-regression diffs don't false-positive on
animation phase):

- **Fixed viewport** — 1520×900 default (override with
  `--width` / `--height`). 1520 matches the existing manual screenshots'
  width so swapping in a generated capture doesn't reflow the README's
  side-by-side image grid.
- **Locale + timezone** — `en-US` / `UTC`. Date columns and
  number formatting stay stable across hosts.
- **Animations off** — every animation/transition is forced to 0s via
  injected CSS so screenshots aren't captured mid-fade.
- **Frozen clock** — `Date.now()` and `new Date()` are pinned to
  `2026-04-26T12:00:00Z` (override with `--freeze-clock` or disable
  with `--no-freeze-clock`). Stops "5 minutes ago" relative timestamps
  from drifting.

## Login flow

The script logs in as `admin` / `admin` by default. If your
`PlatformSettings.force_password_change` is still `true` (default for a
fresh install) the login redirects to a change-password modal and the
script will time out waiting for the navigation to clear `/login`.

Two ways to handle this:
1. Log in once via the UI, change the admin password, then pass the new
   password via `--password 'NewPass!'`.
2. Reset the admin password from the API container per the README's
   "Reset the admin password" snippet, setting
   `force_password_change=False`.

## Troubleshooting

**"chromium not found at /usr/bin/chromium"**
Install the package: `sudo apt-get install -y chromium`. Or point at a
different binary via `PLAYWRIGHT_CHROMIUM_PATH=/path/to/chrome`.

**"Cannot find package 'playwright'"**
Run `cd scripts/screenshots && npm install` (or just `make
screenshots`, which does it for you).

**"login as 'admin' failed — still on /login after submit"**
Wrong password or `force_password_change=true` is set on the user. See
the "Login flow" section above for the two ways to handle this.

**"dev stack not reachable at .../api/v1/health/live"**
Run `make dev` (hot-reload) or `make up` (production images) first.

**Routes render before data loads**
The script waits for `networkidle` + 500 ms, which covers React Query
+ Recharts settling for most pages. Pages with slower-loading server
calls can have a longer settle delay added by tightening their
`waitFor` selector in `routes.mjs` to a data-aware element (e.g.
`table tbody tr` to wait for at least one row).

**Wrong-page screenshots / blank pages**
Run with `--headed` to watch the browser drive the flow. The most
common cause is the `waitFor` selector matching too eagerly — relax it
or replace with a more specific one.

## Adding a new route

1. Add an entry to `routes.mjs`:
   ```js
   { name: "alerts", path: "/admin/alerts", waitFor: "h1, h2" },
   ```
2. Re-run `make screenshots`.
3. Commit the new PNG alongside whatever code change motivated it.
