"""
Test messaging flow: navigates directly to the candidate's profile URL,
clicks More Actions (actionsContainer / ...) → New Message, fills the
message, and sends it.

Sender: Ayesha, Assistant Administrator at Reliable Medical Services.

Usage:
    python scraper/test_messaging.py
"""
import json
import re
import sys
import time
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, CANDIDATES_DIR, CONFIG_DIR, get_logger, load_json

log = get_logger("test_messaging")

CANDIDATE_ID = "indeed-31a1985d7e29"   # VERSATILE (test account)

SETTINGS_FILE = CONFIG_DIR / "settings.json"


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}


def _build_message(first_name: str, role: str) -> str:
    s = _load_settings()
    scheduling_link = s.get("scheduling_link", "")
    sender_name     = s.get("sender_name", "Ayesha")
    sender_title    = s.get("sender_title", "Assistant Administrator")
    company         = s.get("company", "Reliable Medical Services")
    link_line       = f"\n{scheduling_link}\n" if scheduling_link else ""
    return (
        f"Hi {first_name},\n\n"
        f"Thank you for applying for the {role} position at {company}. "
        f"My name is {sender_name} and I am the {sender_title} here.\n\n"
        f"We reviewed your profile and would love to connect with you to learn more "
        f"about your background and share details about the role.\n\n"
        f"Please use the link below to book a quick call at a time that works for you:{link_line}\n"
        f"I look forward to speaking with you soon!\n\n"
        f"Best regards,\n"
        f"{sender_name}\n"
        f"{sender_title}\n"
        f"{company}"
    )


def _pause(lo=0.8, hi=1.8):
    time.sleep(random.uniform(lo, hi))


def snap(page, name: str):
    out = Path("data/screenshots") / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))
    log.info(f"Screenshot: {out}")


def run():
    candidate_file = None
    for f in sorted(CANDIDATES_DIR.glob(f"{CANDIDATE_ID}*.json")):
        candidate_file = f
        break
    if not candidate_file:
        log.error(f"No file found for {CANDIDATE_ID}")
        return

    data        = load_json(candidate_file, {})
    full_name   = data.get("full_name", "")
    job_title   = data.get("job_title", "Administrator")
    profile_url = data.get("indeed_profile_url", "")
    first_name  = full_name.split()[0]
    MESSAGE     = _build_message(first_name, job_title)

    if not profile_url:
        log.error("No indeed_profile_url in candidate file")
        return
    if not SESSION_STATE_FILE.exists():
        log.error("No session. Run: python scraper/indeed_login.py")
        return

    log.info(f"Sending message to {full_name}...")
    log.info(f"Message preview:\n{MESSAGE[:120]}...")

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

        # ── 1. Load profile page ──────────────────────────────────────────────
        page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        _pause(2.5, 4.0)

        # Dismiss popup if present
        try:
            popup = page.get_by_test_id("onboarding-popup-close")
            if popup.count() > 0:
                popup.click()
                _pause(0.5, 1.0)
        except Exception:
            pass

        snap(page, "01_profile_loaded")

        # ── 2. Wait for profile to be interactive ─────────────────────────────
        try:
            page.wait_for_selector("[data-testid='actionsContainer']", timeout=12_000)
        except Exception:
            snap(page, "02_no_actions")
            log.error("actionsContainer not found on profile — see screenshot")
            browser.close()
            return

        # ── 3. Minimize any open messaging windows ────────────────────────────
        for _ in range(3):
            try:
                min_btn = page.get_by_role(
                    "button", name=re.compile(r"Minimize messaging with", re.I)
                )
                if min_btn.count() > 0:
                    min_btn.first.click()
                    _pause(0.3, 0.5)
                else:
                    break
            except Exception:
                break

        # ── 4. Click actionsContainer (... More Actions) ──────────────────────
        page.get_by_test_id("actionsContainer").first.click()
        _pause(0.5, 0.9)
        snap(page, "02_actions_open")

        # ── 5. Click send-new-message ─────────────────────────────────────────
        send_new = page.get_by_test_id("send-new-message")
        if send_new.count() == 0:
            snap(page, "03_no_send_new_message")
            log.error("send-new-message not found — see screenshot")
            browser.close()
            return
        send_new.click()
        _pause(1.2, 2.0)
        snap(page, "03_compose_opened")

        # ── 6. Expand docked messaging window if minimized ────────────────────
        try:
            docked = page.get_by_test_id("indeed-messaging--docked-header")
            if docked.count() > 0:
                docked.click()
                _pause(0.8, 1.2)
                log.info("Expanded docked messaging window")
        except Exception:
            pass

        # Close policy modal if shown
        try:
            close_btn = page.get_by_test_id("messagingPolicyAndTermsModal-closeButton")
            if close_btn.count() > 0:
                close_btn.click()
                _pause(0.4, 0.7)
        except Exception:
            pass

        snap(page, "03b_after_expand")

        # ── 7. Fill compose textarea ──────────────────────────────────────────
        try:
            page.wait_for_selector(
                "[data-testid='indeed-messaging--compose-message-textarea']",
                timeout=10_000,
            )
        except Exception:
            snap(page, "04_no_textarea")
            log.error("Compose textarea not found — see screenshot")
            browser.close()
            return

        ta = page.get_by_test_id("indeed-messaging--compose-message-textarea")
        ta.click()
        ta.press("Control+A")
        ta.fill(MESSAGE)
        _pause(0.8, 1.2)
        snap(page, "04_message_typed")

        # ── 8. Click Send ─────────────────────────────────────────────────────
        send = page.get_by_test_id("indeed-messaging--ComposeBox__sendButton")
        if send.count() == 0:
            snap(page, "05_no_send_button")
            log.error("Send button not found — see screenshot")
            browser.close()
            return

        send.click()
        _pause(1.2, 1.8)
        snap(page, "05_after_send")
        log.info(f"Message sent to {full_name}!")

        browser.close()


if __name__ == "__main__":
    run()
