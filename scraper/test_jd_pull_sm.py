"""
Headed verification: run the real pull_job_descriptions() against SM's still-uncached
jobs so we can watch it click through the jobs list (instead of the old fetch() call
that Cloudflare 403'd) and confirm the JD renders with no challenge.

Usage: python scraper/test_jd_pull_sm.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.company import SM
from agents.context import company_scope

SCREENSHOTS_DIR = Path(__file__).parent.parent / "data" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def snap(page, name: str):
    path = SCREENSHOTS_DIR / f"test_jd_pull_sm_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"  [snap] {path}")


def main():
    if not SM.session_state_file.exists():
        print("ERROR: No SM session. Run: python scraper/auto_login.py --slug sm")
        sys.exit(1)

    try:
        from playwright_stealth.stealth import Stealth as _Stealth
        _stealth = True
    except ImportError:
        _stealth = False

    from playwright.sync_api import sync_playwright

    with company_scope(SM):
        from scraper.indeed_scraper import get_jobs, pull_job_descriptions, JD_FILE

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

            print("Scanning SM jobs...")
            jobs = get_jobs(page)
            print(f"Found {len(jobs)} job(s)")
            snap(page, "jobs_list")

            print("\nPulling job descriptions (click-through)...")
            pull_job_descriptions(page, jobs)
            snap(page, "after_pull")

            browser.close()

    import json
    data = json.loads(JD_FILE.read_text(encoding="utf-8")) if JD_FILE.exists() else {}
    cached = sum(1 for v in data.values() if v.get("job_description"))
    print(f"\nDONE — {cached}/{len(jobs)} job(s) now have a cached JD in {JD_FILE}")
    for j in jobs:
        has = bool(data.get(j["employer_job_id"], {}).get("job_description"))
        print(f"  [{'OK' if has else 'MISSING'}] {j['title']}")


if __name__ == "__main__":
    main()
