"""
One-time interactive login for Indeed employer account (email + OTP flow).

Run this first to create config/session_state.json.
The browser opens visibly — you enter the OTP yourself in the browser.
Once you're on the employer dashboard, press Enter here to save the session.

Usage:
    python scraper/indeed_login.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, Page

from scraper.utils import (
    SESSION_STATE_FILE,
    JOBS_URL,
    get_logger,
    load_credentials,
)

log = get_logger("indeed_login")

EMPLOYER_HOME = "https://employers.indeed.com/"


def try_fill_email(page: Page, email: str):
    """Auto-fill the email field if the login form is visible."""
    try:
        selectors = [
            "input[type='email']",
            "input[name='__email']",
            "input[id*='email']",
            "input[placeholder*='email' i]",
        ]
        for sel in selectors:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.fill(email)
                log.info(f"Email filled: {email}")
                # Submit email form
                page.keyboard.press("Enter")
                page.wait_for_timeout(1500)
                return
    except Exception as e:
        log.debug(f"Auto-fill skipped: {e}")


def is_on_dashboard(page: Page) -> bool:
    url = page.url
    return (
        "employers.indeed.com" in url
        and "secure.indeed.com" not in url
        and "/login" not in url
        and "/signin" not in url
        and "challenge" not in url
    )


def main():
    log.info("Starting Indeed login — email + OTP flow")

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
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
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

        log.info("Opening Indeed employer portal...")
        page.goto(EMPLOYER_HOME, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)

        # Auto-fill email if login form is present
        if not is_on_dashboard(page):
            try_fill_email(page, email)
            page.wait_for_timeout(1000)

        print()
        print("=" * 60)
        print("BROWSER IS OPEN — do the following:")
        print()
        print(f"  1. Make sure the email is filled in: {email}")
        print("     (if not, type it manually in the browser)")
        print()
        print("  2. Check your email inbox for the Indeed OTP code")
        print("     and enter it in the browser")
        print()
        print("  3. Complete any Cloudflare challenge if it appears")
        print()
        print("  4. Wait until you can see your job postings dashboard")
        print()
        print("Then come back here and press Enter to save the session.")
        print("=" * 60)
        input()

        if not is_on_dashboard(page):
            log.error(f"Not on dashboard yet. Current URL: {page.url}")
            log.error("Make sure you're fully logged in, then re-run this script.")
            browser.close()
            sys.exit(1)

        log.info(f"Logged in. URL: {page.url}")

        # Navigate to jobs page to capture all session cookies
        log.info("Navigating to jobs page to capture full session...")
        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)

        if not is_on_dashboard(page):
            print()
            print("Another challenge appeared. Complete it, then press Enter.")
            input()

        # Save the full browser session
        SESSION_STATE_FILE.parent.mkdir(exist_ok=True)
        context.storage_state(path=str(SESSION_STATE_FILE))
        log.info(f"Session saved → {SESSION_STATE_FILE}")

        print()
        print("=" * 60)
        print("Session saved! You won't need to log in again until it expires.")
        print()
        print("Run the scraper now:")
        print("  python scraper/indeed_scraper.py")
        print("=" * 60)

        browser.close()


if __name__ == "__main__":
    main()
