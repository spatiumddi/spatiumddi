"""Take SpatiumDDI UI screenshots for the README / marketing pages.

Requires Playwright:

    pip install playwright
    playwright install chromium

Assumes the stack is running and seeded (run `scripts/seed_demo.py` first).
Logs in via the UI and saves 1920x1080 PNGs to docs/assets/screenshots/.

Usage:
    python scripts/take_screenshots.py http://localhost:8077 admin admin
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets" / "screenshots"

# (route-path-or-selector, output-filename, optional waiting hint)
SHOTS: list[tuple[str, str, str | None]] = [
    ("/dashboard",                "01-dashboard.png",        "text=Overview"),
    ("/ipam",                     "02-ipam-tree.png",        "text=IP Spaces"),
    ("/dns",                      "03-dns.png",              "text=DNS Server Groups"),
    ("/dhcp",                     "04-dhcp.png",             "text=DHCP Server Groups"),
    ("/vlans",                    "05-vlans.png",            "text=Routers"),
    ("/admin/audit",              "06-audit-log.png",        "text=Audit Log"),
    ("/settings",                 "07-settings.png",         "text=Settings"),
    ("/admin/custom-fields",      "08-custom-fields.png",    "text=Custom Fields"),
]


def main(base: str, user: str, pw: str):
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw_ctx:
        browser = pw_ctx.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()

        # ── Login ─────────────────────────────────────────────────────────
        page.goto(f"{base}/login")
        page.fill('input[name="username"], input[type="text"]', user)
        page.fill('input[name="password"], input[type="password"]', pw)
        page.click('button[type="submit"]')
        page.wait_for_url(lambda u: "/login" not in u, timeout=10_000)

        # On first login admin is force-password-changed; skip that if prompted.
        if "change-password" in page.url:
            print("! Account requires password change — set it manually then re-run.")
            browser.close()
            return

        for route, fname, hint in SHOTS:
            print(f"→ {route} → {fname}")
            page.goto(f"{base}{route}")
            if hint:
                try:
                    page.wait_for_selector(hint, timeout=5_000)
                except Exception:
                    pass
            page.wait_for_timeout(800)  # let animations / charts settle
            page.screenshot(path=str(OUT / fname), full_page=False)

        # IPAM — drill into a specific subnet for a detail shot.
        try:
            page.goto(f"{base}/ipam")
            page.wait_for_timeout(500)
            # Expand the first space, then click the first subnet.
            page.click('text="Corporate"', timeout=3_000)
            page.wait_for_timeout(300)
            page.click('text="Servers"', timeout=3_000)
            page.wait_for_timeout(800)
            page.screenshot(path=str(OUT / "09-subnet-detail.png"), full_page=False)
        except Exception as e:
            print(f"! subnet detail shot skipped: {e}")

        browser.close()
    print(f"\n✓ Screenshots saved to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python scripts/take_screenshots.py <base_url> <username> <password>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
