"""
Quick Playwright test: navigate to an SM job page, extract the JD, screenshot.
Usage: python scraper/test_jd_extraction.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.company import SM
from scraper.indeed_scraper import _extract_jd_text

SCREENSHOTS_DIR = Path(__file__).parent.parent / "data" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

def main():
    if not SM.session_state_file.exists():
        print("ERROR: No SM session. Run: python scraper/auto_login.py --slug sm")
        sys.exit(1)

    # Grab one SM job_id from a candidate file
    cands = list(SM.candidates_dir.glob("*.json"))
    if not cands:
        print("ERROR: No SM candidate files found.")
        sys.exit(1)

    d = json.loads(cands[0].read_text(encoding="utf-8"))
    job_id = d.get("job_id", "")
    job_title = d.get("job_title", "unknown")
    if not job_id:
        print("ERROR: candidate file has no job_id")
        sys.exit(1)

    url = f"https://employers.indeed.com/jobs/view?employerJobId={job_id}"
    print(f"Testing JD extraction for: {job_title}")
    print(f"URL: {url}")

    try:
        from playwright_stealth.stealth import Stealth as _Stealth
        _stealth = True
    except ImportError:
        _stealth = False

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            storage_state=str(SM.session_state_file),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        if _stealth:
            _Stealth().apply_stealth_sync(page)

        print("Navigating...")
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Wait for job content to render (lazy-loaded JS)
        try:
            page.wait_for_selector(
                ".jd-appended-job-description, .css-1jpbxfu, .css-19qk1my, "
                ".css-15c5oio, [data-testid='job-description'], h1",
                timeout=12_000,
            )
        except Exception:
            pass
        page.wait_for_timeout(4000)  # extra buffer for lazy sections

        # Screenshot after page has loaded
        snap_path = SCREENSHOTS_DIR / "test_jd_before.png"
        page.screenshot(path=str(snap_path), full_page=True)
        print(f"Screenshot saved: {snap_path}")

        # Try each selector manually so we can see which one fires
        selectors = [
            ".jd-appended-job-description",
            ".css-1jpbxfu",
            ".css-19qk1my",
            ".css-15c5oio",
        ]
        print("\nSelector probe:")
        for sel in selectors:
            try:
                els = page.locator(sel).all()
                texts = [e.inner_text().strip()[:80] for e in els if e.inner_text().strip()]
                if texts:
                    print(f"  {sel}: {len(texts)} element(s) — first: {texts[0]!r}")
                else:
                    print(f"  {sel}: 0 elements")
            except Exception as e:
                print(f"  {sel}: ERROR {e}")

        # Full extraction
        jd = _extract_jd_text(page)
        # Final screenshot
        final_snap = SCREENSHOTS_DIR / "test_jd_result.png"
        page.screenshot(path=str(final_snap), full_page=True)
        print(f"Final screenshot: {final_snap}")

        if jd:
            print(f"\nOK JD extracted ({len(jd)} chars):\n")
            print(jd)
        else:
            print("\nFAIL JD extraction returned empty string")

        browser.close()

if __name__ == "__main__":
    main()
