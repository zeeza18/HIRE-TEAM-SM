"""
Test rejection flow: navigates directly to a candidate's profile URL
and clicks ApplicantSentiment-no (thumbs down) to reject on Indeed.

Usage:
    python scraper/test_rejection.py
"""
import sys
import time
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, CANDIDATES_DIR, get_logger, load_json

log = get_logger("test_rejection")

CANDIDATE_ID = "indeed-45b4e8a2c967"   # GABRIELA GUZMAN


def _pause(lo=0.8, hi=1.8):
    time.sleep(random.uniform(lo, hi))


def snap(page, name: str):
    out = Path("data/screenshots") / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))
    log.info(f"Screenshot: {out}")


def run():
    # Find candidate file
    candidate_file = None
    for f in sorted(CANDIDATES_DIR.glob(f"{CANDIDATE_ID}*.json")):
        candidate_file = f
        break
    if not candidate_file:
        log.error(f"No file found for {CANDIDATE_ID}")
        return

    data = load_json(candidate_file, {})
    full_name   = data.get("full_name", "")
    profile_url = data.get("indeed_profile_url", "")

    if not profile_url:
        log.error("No indeed_profile_url in candidate file")
        return
    if not SESSION_STATE_FILE.exists():
        log.error("No session. Run: python scraper/indeed_login.py")
        return

    log.info(f"Rejecting {full_name} via profile URL...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(SESSION_STATE_FILE),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        _pause(2.5, 4.0)

        snap(page, "01_profile_loaded")

        # Dismiss popup if present
        try:
            popup = page.get_by_test_id("onboarding-popup-close")
            if popup.count() > 0:
                popup.click()
                _pause(0.5, 1.0)
        except Exception:
            pass

        # Click ApplicantSentiment-no (thumbs down = reject)
        btn = page.get_by_test_id("ApplicantSentiment-no")
        if btn.count() == 0:
            snap(page, "02_no_sentiment_button")
            log.error("ApplicantSentiment-no button not found — see screenshot")
            browser.close()
            return

        log.info("Found ApplicantSentiment-no — clicking...")
        btn.click()
        _pause(1.0, 1.5)
        snap(page, "03_after_reject_click")

        log.info(f"Done — {full_name} marked as rejected on Indeed.")
        browser.close()


if __name__ == "__main__":
    run()
