"""
Test messaging flow: navigates to a candidate's profile URL, opens the
More Actions → New Message panel, fills in the outreach message, and sends it.

Sender: Ayesha, Assistant Administrator at Reliable Medical Services.
Message: asks candidate for the best time and number to reach them.

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

CANDIDATE_ID = "indeed-50bda6094469"   # SHIFA FARHEEN

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

    first_name = full_name.split()[0]
    role       = data.get("job_title", "Administrator")
    MESSAGE    = _build_message(first_name, role)

    log.info(f"Sending message to {full_name} via profile URL...")
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

        # ── 2. Wait for candidate list panel ──────────────────────────────────
        try:
            page.wait_for_selector(
                "[data-testid='candidate-list-table-container']", timeout=15_000
            )
            log.info("Candidate list panel found")
        except Exception:
            snap(page, "02_no_list_panel")
            log.error("Candidate list panel not found — see screenshot")
            browser.close()
            return

        snap(page, "02_list_panel_ready")

        # ── 3. Find and hover the candidate row ───────────────────────────────
        rows = page.locator(
            "[data-testid='candidate-list-table-container'] [role='row']"
        ).all()

        target_row = None
        for row in rows:
            try:
                if full_name.lower() in row.inner_text().lower():
                    target_row = row
                    break
            except Exception:
                continue

        if target_row is None:
            snap(page, "03_candidate_not_in_list")
            log.error(f"{full_name!r} not found in candidate list — see screenshot")
            browser.close()
            return

        log.info(f"Found row for {full_name!r} — hovering...")
        target_row.hover()
        _pause(0.5, 0.9)
        snap(page, "03_row_hovered")

        # ── 4. Click More Actions ─────────────────────────────────────────────
        clicked_actions = False
        for try_loc in [
            lambda r: r.get_by_role("cell", name="More Actions"),
            lambda r: r.locator("[data-testid='actionsContainer']"),
        ]:
            try:
                el = try_loc(target_row)
                if el.count() > 0:
                    el.first.click()
                    clicked_actions = True
                    break
            except Exception:
                continue

        if not clicked_actions:
            # Last resort: last cell in row
            try:
                cells = target_row.locator("td").all()
                if cells:
                    cells[-1].click()
                    clicked_actions = True
            except Exception:
                pass

        if not clicked_actions:
            snap(page, "04_no_more_actions")
            log.error("Could not click More Actions — see screenshot")
            browser.close()
            return

        _pause(0.5, 0.9)
        snap(page, "04_more_actions_clicked")

        # ── 5. Minimize any open messaging windows ────────────────────────────
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

        # ── 6. Click send-new-message ─────────────────────────────────────────
        opened = False
        for attempt in range(2):
            try:
                btn = page.get_by_test_id("send-new-message")
                if btn.count() > 0:
                    btn.click()
                    _pause(1.2, 2.0)
                    opened = True
                    break
                if attempt == 0:
                    # Re-open the actions menu and try again
                    try:
                        page.locator("[data-testid='actionsContainer']").first.click()
                        _pause(0.5, 0.9)
                    except Exception:
                        pass
            except Exception:
                pass

        if not opened:
            snap(page, "05_no_send_new_message")
            log.error("send-new-message button not found — see screenshot")
            browser.close()
            return

        snap(page, "05_compose_opened")

        # ── 7. Close policy modal if shown ────────────────────────────────────
        try:
            close_btn = page.get_by_test_id("messagingPolicyAndTermsModal-closeButton")
            if close_btn.count() > 0:
                close_btn.click()
                _pause(0.4, 0.7)
        except Exception:
            pass

        # ── 8. Fill compose textarea ──────────────────────────────────────────
        try:
            page.wait_for_selector(
                "[data-testid='indeed-messaging--compose-message-textarea']",
                timeout=10_000,
            )
        except Exception:
            snap(page, "06_no_compose_textarea")
            log.error("Compose textarea not found — see screenshot")
            browser.close()
            return

        ta = page.get_by_test_id("indeed-messaging--compose-message-textarea")
        ta.click()
        ta.press("Control+A")
        ta.fill(MESSAGE)
        _pause(0.8, 1.2)
        snap(page, "06_message_typed")

        # ── 9. Click Send ─────────────────────────────────────────────────────
        sent = False
        for nth in [1, 2, 0]:
            try:
                send_el = page.locator("div").filter(
                    has_text=re.compile(r"^Send$")
                ).nth(nth)
                if send_el.count() > 0:
                    send_el.click()
                    _pause(1.2, 1.8)
                    sent = True
                    break
            except Exception:
                pass

        if not sent:
            try:
                btn = page.get_by_role("button", name=re.compile(r"^Send$"))
                if btn.count() > 0:
                    btn.first.click()
                    _pause(1.2, 1.8)
                    sent = True
            except Exception:
                pass

        snap(page, "07_after_send")

        if sent:
            log.info(f"Message sent to {full_name}!")
        else:
            log.error("Could not find Send button — message may not have sent, check screenshot")

        browser.close()


if __name__ == "__main__":
    run()
