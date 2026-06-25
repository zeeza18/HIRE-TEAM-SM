"""
Debug: navigate to SM job page in HEADLESS mode (matching production),
dump all page text and screenshot so we can see what's actually rendered.
Usage: python scraper/debug_jd_headless.py
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.company import SM
from scraper.utils import get_logger

log = get_logger("debug_jd")
SCREENSHOTS = Path(__file__).parent.parent / "data" / "screenshots"
SCREENSHOTS.mkdir(parents=True, exist_ok=True)

def snap(page, name):
    p = SCREENSHOTS / f"debug_jd_{name}.png"
    page.screenshot(path=str(p), full_page=True)
    print(f"  [snap] {p.name}")

def main():
    if not SM.session_state_file.exists():
        print("ERROR: No SM session"); sys.exit(1)

    try:
        from playwright_stealth.stealth import Stealth as _Stealth
        _stealth = True
    except ImportError:
        _stealth = False

    from playwright.sync_api import sync_playwright

    # Get a job ID from SM jobs page
    JOBS_URL = "https://employers.indeed.com/jobs"
    job_id = None
    job_title = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,  # match production
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            storage_state=str(SM.session_state_file),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        if _stealth:
            _Stealth().apply_stealth_sync(page)

        # Navigate to jobs list to get a real job ID
        print("Getting job list...")
        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
        try:
            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        snap(page, "01_jobs_list")

        links = page.locator("[data-testid='UnifiedJobTldLink']").all()
        print(f"  Found {len(links)} job links")
        for link in links[:3]:
            href = link.get_attribute("href") or ""
            title = link.inner_text().strip()
            if "employerJobId=" in href:
                from urllib.parse import urlparse, parse_qs
                eid = parse_qs(urlparse(href).query).get("employerJobId", [""])[0]
                if eid:
                    job_id = eid
                    job_title = title
                    print(f"  Using job: {title} (ID={eid})")
                    break

        if not job_id:
            print("ERROR: No job ID found"); browser.close(); return

        # === APPROACH: click the job link (not direct URL navigation) ===
        from urllib.parse import unquote
        print(f"\nClicking job link for: {job_title}")
        # Print all hrefs to debug
        all_links = page.locator("[data-testid='UnifiedJobTldLink']").all()
        print(f"  Links on page: {len(all_links)}")
        for i, link in enumerate(all_links[:3]):
            raw_href = link.get_attribute("href") or ""
            print(f"    [{i}] text={link.inner_text().strip()!r}  href (raw)={raw_href[:80]!r}")
            print(f"         href (decoded)={unquote(raw_href)[:80]!r}")

        clicked = False
        for link in page.locator("[data-testid='UnifiedJobTldLink']").all():
            href = unquote(link.get_attribute("href") or "")
            if job_id in href:
                print(f"  Matched by ID in href!")
                link.click()
                clicked = True
                break
        if not clicked:
            for link in page.locator("[data-testid='UnifiedJobTldLink']").all():
                if link.inner_text().strip() == job_title:
                    print(f"  Matched by title text!")
                    link.click()
                    clicked = True
                    break
        if not clicked:
            print("  WARN: No match found, falling back to direct URL")
            url = f"https://employers.indeed.com/jobs/view?employerJobId={job_id}"
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        snap(page, "02_job_view_after_click")

        # Wait for content
        try:
            page.wait_for_selector(
                ".jd-appended-job-description, .css-1jpbxfu, .css-19qk1my, "
                ".css-15c5oio, [data-testid='job-description'], h1",
                timeout=12_000,
            )
        except Exception:
            pass
        page.wait_for_timeout(3000)
        snap(page, "03_after_wait")

        # Dump all body text
        body_text = page.inner_text("body")
        sys.stdout.buffer.write(f"\n=== BODY TEXT ({len(body_text)} chars) ===\n".encode("utf-8"))
        sys.stdout.buffer.write(body_text[:5000].encode("utf-8"))
        sys.stdout.buffer.write(b"\n")

        # Save full text to file
        text_file = SCREENSHOTS / "debug_jd_body_text.txt"
        text_file.write_text(body_text, encoding="utf-8")
        print(f"\nFull text saved to: {text_file}")

        # Check selectors
        print("\n=== SELECTOR CHECK ===")
        for sel in [".jd-appended-job-description", ".css-1jpbxfu", ".css-19qk1my", ".css-15c5oio"]:
            count = page.locator(sel).count()
            print(f"  {sel}: {count} element(s)")

        # Check if "Job description" text exists
        if "Job description" in body_text:
            idx = body_text.find("Job description")
            sys.stdout.buffer.write(f"\n'Job description' found at char {idx}\n".encode("utf-8"))
            sys.stdout.buffer.write(f"  Context: {body_text[max(0,idx-50):idx+300]}\n".encode("utf-8"))
        else:
            print("\n'Job description' NOT found in page text")

        # Try _extract_jd_from_body
        from scraper.indeed_scraper import _extract_jd_from_body, _extract_jd_text
        jd_body = _extract_jd_from_body(body_text)
        print(f"\n_extract_jd_from_body: {len(jd_body)} chars")
        if jd_body:
            sys.stdout.buffer.write(jd_body[:500].encode("utf-8"))
            sys.stdout.buffer.write(b"\n")

        jd_sel = _extract_jd_text(page)
        print(f"\n_extract_jd_text (selector): {len(jd_sel)} chars")

        browser.close()

if __name__ == "__main__":
    main()
