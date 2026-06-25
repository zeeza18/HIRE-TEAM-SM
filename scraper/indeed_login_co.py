"""
Company-specific Indeed login launcher.

Opens a real browser — you log in manually, then press Enter to save the session.

Usage:
    python scraper/indeed_login_co.py --slug rms
    python scraper/indeed_login_co.py --slug sm
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

START_URL = "https://www.indeed.com/hire"
JOBS_URL  = (
    "https://employers.indeed.com/jobs"
    "?status=open%2Cpaused&claimed=false&createdOnIndeed=true"
    "&tab=0&sortDirection=DESC&sortField=datePostedOnIndeed"
)


def main(slug: str):
    from agents.company import COMPANIES
    co = COMPANIES.get(slug)
    if not co:
        print(f"Unknown company slug: {slug}"); sys.exit(1)

    creds_file = co.credentials_file
    if not creds_file.exists():
        print(f"Credentials file not found: {creds_file}"); sys.exit(1)

    creds = json.loads(creds_file.read_text(encoding="utf-8"))
    email = creds.get("indeed_email", "").strip()
    if not email or email == "your@email.com":
        print(f"Set indeed_email in {creds_file}"); sys.exit(1)

    session_file = co.session_state_file
    session_file.parent.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    print(f"\n{'='*60}")
    print(f"  Indeed Login — {co.display_name}")
    print(f"  Account: {email}")
    print(f"{'='*60}")
    print()
    print("  A browser will open on www.indeed.com/hire")
    print(f"  1. Click Sign in -> enter {email} -> get OTP -> log in")
    print("  2. After login, go to employers.indeed.com so employer cookies are captured")
    print("  3. Come back here and press Enter")
    print()

    import os, subprocess, tempfile, time

    chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not os.path.exists(chrome_exe):
        chrome_exe = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

    # Use a fresh temp profile so it never conflicts with running Chrome
    temp_dir = tempfile.mkdtemp(prefix="indeed_login_")
    debug_port = 9222

    print(f"Launching Chrome on port {debug_port}...")
    proc = subprocess.Popen([
        chrome_exe,
        f"--user-data-dir={temp_dir}",
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "--disable-blink-features=AutomationControlled",
        "https://www.indeed.com/hire",
    ])
    time.sleep(3)   # give Chrome a moment to start

    with sync_playwright() as p:
        browser  = p.chromium.connect_over_cdp(f"http://localhost:{debug_port}")
        context  = browser.contexts[0] if browser.contexts else browser.new_context()
        pages    = context.pages
        page     = pages[0] if pages else context.new_page()

        print("Opening Indeed (www.indeed.com/hire — avoids bot detection)...")
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)

        print()
        print("="*60)
        print(f"  Browser is open. Log in with: {email}")
        print()
        print("  When you see the jobs/dashboard page, press Enter here.")
        print("="*60)
        input("  > Press Enter once logged in: ")

        # Save immediately so a closed browser doesn't lose the session
        print("\nCapturing session...")
        context.storage_state(path=str(session_file))

        # Try navigating to jobs page to capture employer cookies, then save again
        try:
            if "employers.indeed.com" not in page.url:
                page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3000)
            context.storage_state(path=str(session_file))
        except Exception:
            pass  # browser closed or already there — initial save is enough

        print()
        print("="*60)
        print(f"  Session saved: {session_file}")
        print()
        print("  Close this window. The portal will detect the session")
        print("  and start scraping within 15 seconds.")
        print("="*60)
        print()
        input("  Press Enter to close the browser: ")

        try:
            browser.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Indeed login for a specific company")
    parser.add_argument("--slug", required=True, choices=["rms", "sm"],
                        help="Company slug: rms or sm")
    args = parser.parse_args()
    main(args.slug)
