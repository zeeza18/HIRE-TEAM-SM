"""
Interactive login for Indeed employer account — iframe OTP flow.

Recorded with playwright codegen and hardened with error handling.
Opens a real browser window, auto-fills email, switches to OTP login,
prompts you to paste the code, then saves the session.

Usage:
    python scraper/indeed_login.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from scraper.utils import SESSION_STATE_FILE, JOBS_URL, get_logger, load_credentials

log = get_logger("indeed_login")

HIRE_URL = "https://www.indeed.com/hire/cs?from=orchestrator&hl=en"


def main():
    try:
        creds = load_credentials()
        email = creds.get("indeed_email", "").strip()
        if not email or email == "your@email.com":
            log.error("Set your Indeed email in config/credentials.json first.")
            sys.exit(1)
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        context = browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        log.info("Opening Indeed hire portal...")
        page.goto(HIRE_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)

        # Cloudflare challenge often appears on first load — wait for user to clear it
        if "challenge" in page.url or "cloudflare" in page.content().lower():
            print()
            print("=" * 60)
            print("  Cloudflare detected — complete the check in the browser,")
            print("  then press Enter to continue.")
            print("=" * 60)
            input()
            page.wait_for_timeout(1500)

        log.info("Clicking Sign in...")
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_timeout(1500)

        # Login form lives inside an iframe
        frame = page.locator("iframe[title='Login Form']").content_frame

        log.info(f"Filling email: {email}")
        email_input = frame.get_by_role("textbox", name="Email address")
        email_input.click()
        email_input.fill(email)
        page.wait_for_timeout(500)

        frame.get_by_test_id("modal-view-footer").get_by_role("button", name="Continue").click()
        page.wait_for_timeout(1500)

        log.info("Switching to OTP login...")
        frame.get_by_role("link", name="Sign in with a code instead").click()
        page.wait_for_timeout(1000)

        print()
        print("=" * 60)
        print(f"  OTP sent to: {email}")
        print()
        print("  Option A: Paste the code here and press Enter (auto-fills browser)")
        print("  Option B: Type it directly in the browser, then press Enter here")
        print("=" * 60)
        otp = input("  OTP code (or just press Enter if you filled it in browser): ").strip()
        print()

        if otp:
            frame = page.locator("iframe[title='Login Form']").content_frame
            otp_input = frame.get_by_role("textbox", name="Enter code")
            otp_input.click()
            otp_input.fill(otp)
            page.wait_for_timeout(500)
            otp_input.press("Enter")
            log.info("OTP submitted — waiting for dashboard...")
            page.wait_for_timeout(3000)
        else:
            log.info("Waiting for manual login to complete...")
            page.wait_for_timeout(2000)
        page.wait_for_timeout(3000)

        # Navigate to jobs page to capture full session cookies
        log.info("Loading jobs page to capture full session...")
        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)

        # Handle any follow-up Cloudflare / security challenge
        if "challenge" in page.url or "secure.indeed.com" in page.url or "/login" in page.url:
            print()
            print("Challenge detected — complete it in the browser, then press Enter.")
            input()

        SESSION_STATE_FILE.parent.mkdir(exist_ok=True)
        context.storage_state(path=str(SESSION_STATE_FILE))
        log.info(f"Session saved → {SESSION_STATE_FILE}")

        print()
        print("=" * 60)
        print("  Session saved. You won't need to log in again until it expires.")
        print()
        print("  Run the scraper:")
        print("    python scraper/indeed_scraper.py")
        print("=" * 60)

        browser.close()


if __name__ == "__main__":
    main()
