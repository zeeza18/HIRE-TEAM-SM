"""
Debug script — dumps all links and table HTML from the jobs page.
Run: python scraper/debug_jobs.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, JOBS_URL

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context(storage_state=str(SESSION_STATE_FILE))
    page = context.new_page()

    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3000)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    print("\n=== CLICKING 'New' CANDIDATES BUTTON ON FIRST JOB ===")
    new_btn = page.locator("[data-testid='candidates-pipeline-hosted-new-link']").first
    print(f"  Button found: {new_btn.count() > 0}")
    if new_btn.count() > 0:
        new_btn.click()
        page.wait_for_timeout(3000)
        print(f"  URL after click: {page.url}")
        page.screenshot(path=str(Path(__file__).parent.parent / "data" / "debug_candidates.png"), full_page=True)
        print("  Screenshot saved → data/debug_candidates.png")

    screenshot_path = Path(__file__).parent.parent / "data" / "debug_screenshot.png"
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_path), full_page=True)
    print(f"\nScreenshot saved → {screenshot_path}")

    input("\nPress Enter to close browser...")
    browser.close()
