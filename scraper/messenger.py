"""
Indeed Messenger — sends messages and schedules interviews on Indeed via Playwright.
Called by the Flask backend when the recruiter clicks Send in the portal.
"""

import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, Page

from scraper.utils import (
    SESSION_STATE_FILE, CANDIDATES_DIR, JOBS_URL, CONFIG_DIR,
    get_logger, load_json, save_json, now_iso,
)

log = get_logger("messenger")
TEMPLATES_FILE = CONFIG_DIR / "message_templates.json"


# ── tiny helpers (duplicated from scraper to avoid circular import) ─────────

def _pause(lo: float = 0.8, hi: float = 1.8):
    time.sleep(random.uniform(lo, hi))


def _dismiss_popup(page: Page):
    try:
        p = page.locator("[data-testid='onboarding-popup-close']")
        if p.count() > 0:
            p.click()
            page.wait_for_timeout(300)
    except Exception:
        pass


# ── templates ────────────────────────────────────────────────────────────────

def _load_templates() -> dict:
    if TEMPLATES_FILE.exists():
        return json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    return {"interview_invite": [], "rejection": []}


def _fill_template(body: str, candidate: dict) -> str:
    first_name = (candidate.get("full_name") or "").split()[0]
    role = candidate.get("job_title") or "the position"
    return (
        body
        .replace("{{first_name}}", first_name)
        .replace("{{name}}", candidate.get("full_name") or "")
        .replace("{{role}}", role)
        .replace("{{company}}", "Reliable Medical Services")
    )


def get_templates() -> dict:
    """Return the raw templates (for /api/message-templates endpoint)."""
    return _load_templates()


def get_drafts(candidate_id: str) -> dict:
    """Return pre-filled draft messages for a candidate."""
    path = _find_candidate_file(candidate_id)
    if not path:
        return {"error": f"Candidate {candidate_id} not found"}
    candidate = load_json(path, {})
    templates = _load_templates()
    return {
        "interview_invite": [
            {"id": t["id"], "label": t["label"], "body": _fill_template(t["body"], candidate)}
            for t in templates.get("interview_invite", [])
        ],
        "rejection": [
            {"id": t["id"], "label": t["label"], "body": _fill_template(t["body"], candidate)}
            for t in templates.get("rejection", [])
        ],
        "candidate_name": candidate.get("full_name"),
        "role": candidate.get("job_title"),
    }


# ── candidate file lookup ─────────────────────────────────────────────────────

def _find_candidate_file(candidate_id: str) -> Path | None:
    # Files are "indeed-xxx-jobhash.json"; candidate_id is "indeed-xxx"
    for f in sorted(CANDIDATES_DIR.glob(f"{candidate_id}-*.json")):
        return f
    exact = CANDIDATES_DIR / f"{candidate_id}.json"
    if exact.exists():
        return exact
    # Also try exact match without suffix
    for f in sorted(CANDIDATES_DIR.glob(f"{candidate_id}*.json")):
        return f
    return None


# ── navigation helpers ────────────────────────────────────────────────────────

def _browser_context(p):
    return p.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    ).new_context(
        storage_state=str(SESSION_STATE_FILE),
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )


def _navigate_to_all_candidates(page: Page, job_id: str, job_title: str) -> bool:
    """Navigate to All-candidates list for the given job. Returns True on success."""
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)
    try:
        page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=30_000, state="attached")
    except Exception:
        return False

    job_link = None
    for lnk in page.locator("[data-testid='UnifiedJobTldLink']").all():
        href = lnk.get_attribute("href") or ""
        eid = parse_qs(urlparse(href).query).get("employerJobId", [""])[0]
        if eid == job_id:
            job_link = lnk
            break
    if job_link is None:
        for lnk in page.locator("[data-testid='UnifiedJobTldLink']").all():
            if job_title and job_title.lower() in (lnk.inner_text() or "").lower():
                job_link = lnk
                break

    if job_link is None:
        log.error(f"Could not find job link for {job_title!r}")
        return False

    job_link.click()
    _pause(2.5, 4.0)
    _dismiss_popup(page)

    for sel in [
        "[data-testid='candidates-pipeline-hosted-all-link']",
        "[data-testid='menu-link-Candidates']",
    ]:
        try:
            nav = page.locator(sel).first
            if nav.count() > 0:
                nav.click()
                _pause(1.5, 2.5)
                break
        except Exception:
            pass

    try:
        page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=15_000)
    except Exception:
        log.error("Candidates table not found after navigation")
        return False

    # Click "All applications" to show every candidate regardless of status/stage
    try:
        all_lbl = page.locator("label").filter(has_text="All applications")
        if all_lbl.count() > 0:
            all_lbl.first.click()
            _pause(1.5, 2.5)
            log.info("Clicked 'All applications' filter")
    except Exception:
        pass

    return True


# ── messaging flow ────────────────────────────────────────────────────────────

def _open_messaging_from_profile(page: Page, profile_url: str, full_name: str) -> bool:
    """
    Navigate to the candidate's profile URL (which loads the list with the
    correct filter — including Rejected — in the left panel), then use the
    standard More Actions → send-new-message flow on that row.
    """
    page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    _pause(2.0, 3.5)
    _dismiss_popup(page)

    try:
        page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=15_000)
    except Exception:
        log.error("Candidate list panel not found on profile page")
        return False

    log.info(f"Profile page loaded with list panel — searching for {full_name!r}")
    return _open_messaging_for(page, full_name)


def _open_messaging_for(page: Page, candidate_name: str) -> bool:
    """
    Paginate through candidate list, hover over the matching row,
    click More Actions → send-new-message. Returns True on success.
    """
    page_num = 0
    while True:
        page_num += 1
        rows = page.locator(
            "[data-testid='candidate-list-table-container'] [role='row']"
        ).all()

        for row in rows:
            try:
                text = row.inner_text().strip()
            except Exception:
                continue
            if candidate_name.lower() not in text.lower():
                continue

            # Hover to reveal More Actions
            try:
                row.hover()
                _pause(0.4, 0.7)
            except Exception:
                pass

            # Click More Actions cell
            clicked = False
            for try_loc in [
                lambda r: r.get_by_role("cell", name="More Actions"),
                lambda r: r.locator("[data-testid='actionsContainer']"),
            ]:
                try:
                    el = try_loc(row)
                    if el.count() > 0:
                        el.first.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                # Last resort: click last cell of the row
                try:
                    cells = row.locator("td").all()
                    if cells:
                        cells[-1].click()
                        clicked = True
                except Exception:
                    pass

            if not clicked:
                log.error(f"Could not click More Actions for {candidate_name!r}")
                return False

            _pause(0.5, 0.9)

            # Minimize any open messaging windows
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

            # Click send-new-message
            for attempt in range(2):
                try:
                    btn = page.get_by_test_id("send-new-message")
                    if btn.count() > 0:
                        btn.click()
                        _pause(1.0, 1.8)
                        return True
                    if attempt == 0:
                        try:
                            page.locator("[data-testid='actionsContainer']").first.click()
                            _pause(0.5, 0.8)
                        except Exception:
                            pass
                except Exception:
                    pass

            log.error("send-new-message button not found after clicking More Actions")
            return False

        # Next page
        next_btn = page.locator("button:has-text('Next'), a:has-text('Next')").last
        if next_btn.count() > 0 and next_btn.is_enabled():
            next_btn.click()
            _pause(2.0, 3.0)
        else:
            break

    log.error(f"{candidate_name!r} not found in list after {page_num} page(s)")
    return False


def _compose_and_send(page: Page, message: str) -> bool:
    """Fill compose textarea and click Send. Returns True on success."""
    # Close policy modal if shown
    try:
        close_btn = page.get_by_test_id("messagingPolicyAndTermsModal-closeButton")
        if close_btn.count() > 0:
            close_btn.click()
            _pause(0.4, 0.7)
    except Exception:
        pass

    try:
        page.wait_for_selector(
            "[data-testid='indeed-messaging--compose-message-textarea']",
            timeout=10_000,
        )
    except Exception:
        log.error("Compose textarea not found")
        return False

    ta = page.get_by_test_id("indeed-messaging--compose-message-textarea")
    ta.click()
    ta.press("Control+A")
    ta.fill(message)
    _pause(0.5, 0.9)

    # Try Send div (nth(1) from codegen, then fallback options)
    for nth in [1, 2, 0]:
        try:
            send_el = page.locator("div").filter(
                has_text=re.compile(r"^Send$")
            ).nth(nth)
            if send_el.count() > 0:
                send_el.click()
                _pause(1.0, 1.5)
                return True
        except Exception:
            pass

    try:
        btn = page.get_by_role("button", name=re.compile(r"^Send$"))
        if btn.count() > 0:
            btn.first.click()
            _pause(1.0, 1.5)
            return True
    except Exception:
        pass

    log.error("Could not find Send button in compose window")
    return False


# ── rejection via ApplicantSentiment-no ──────────────────────────────────────

def _reject_on_profile(page: Page, profile_url: str) -> bool:
    """Navigate to the candidate profile and click the thumbs-down reject button."""
    page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)
    _dismiss_popup(page)

    try:
        page.wait_for_selector("[data-testid='ApplicantSentiment-no']", timeout=12_000)
    except Exception:
        log.error("ApplicantSentiment-no not found on profile page")
        return False

    page.get_by_test_id("ApplicantSentiment-no").click()
    _pause(1.0, 1.5)
    log.info("Clicked ApplicantSentiment-no (rejected on Indeed)")
    return True


# ── public API ────────────────────────────────────────────────────────────────

def send_message(candidate_id: str, message: str, new_status: str = "contacting") -> dict:
    """
    Send a message to a candidate on Indeed via the employer messaging panel.
    Opens a visible browser window, navigates to their job's candidate list,
    finds the row, and sends the message.
    Returns {"ok": bool, "status": str?, "error": str?}
    """
    path = _find_candidate_file(candidate_id)
    if not path:
        return {"ok": False, "error": f"Candidate file not found for {candidate_id}"}

    candidate = load_json(path, {})
    full_name   = candidate.get("full_name", "")
    job_id      = candidate.get("job_id", "")
    job_title   = candidate.get("job_title", "")
    profile_url = candidate.get("indeed_profile_url", "")

    if not SESSION_STATE_FILE.exists():
        return {"ok": False, "error": "No session. Run: python scraper/indeed_login.py"}

    try:
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

            # For rejections: click ApplicantSentiment-no on the profile page
            if new_status == "rejected" and profile_url:
                if not _reject_on_profile(page, profile_url):
                    browser.close()
                    return {"ok": False, "error": "Could not click reject button on Indeed profile"}
                browser.close()
            else:
                # For other statuses: open the messaging compose window
                if profile_url:
                    opened = _open_messaging_from_profile(page, profile_url, full_name)
                else:
                    opened = False

                if not opened:
                    if not _navigate_to_all_candidates(page, job_id, job_title):
                        browser.close()
                        return {"ok": False, "error": "Could not navigate to candidates list"}
                    opened = _open_messaging_for(page, full_name)

                if not opened:
                    browser.close()
                    return {"ok": False, "error": f"Could not open messaging for {full_name!r}"}

                if not _compose_and_send(page, message):
                    browser.close()
                    return {"ok": False, "error": "Message compose/send step failed"}

                browser.close()

        candidate["status"] = new_status
        if new_status == "rejected":
            candidate["indeed_rejected"] = True
            candidate["indeed_rejected_at"] = now_iso()
        else:
            candidate["indeed_message_sent"] = True
            candidate["indeed_message_sent_at"] = now_iso()
        save_json(path, candidate)

        log.info(f"{'Rejected' if new_status == 'rejected' else 'Message sent'} → {full_name} (status={new_status})")
        return {"ok": True, "status": new_status}

    except Exception as e:
        log.error(f"send_message error: {e}")
        return {"ok": False, "error": str(e)}


def schedule_interview(
    candidate_id: str,
    message: str,
    interview_date: str,    # "YYYY-MM-DD"
    start_time: str,        # "HH:MM" 24-hour
    duration: str = "30",   # minutes
    format_: str = "Phone",
) -> dict:
    """
    Use Indeed's native Schedule Interview feature for a candidate.
    Navigates directly to their stored profile URL, clicks Schedule Interview,
    fills the form, and sends. Returns {"ok": bool, "status": str?, "error": str?}
    """
    path = _find_candidate_file(candidate_id)
    if not path:
        return {"ok": False, "error": f"Candidate file not found for {candidate_id}"}

    candidate = load_json(path, {})
    full_name   = candidate.get("full_name", "")
    profile_url = candidate.get("indeed_profile_url", "")

    if not profile_url:
        return {"ok": False, "error": "No indeed_profile_url stored — re-scrape this candidate first"}

    if not SESSION_STATE_FILE.exists():
        return {"ok": False, "error": "No session. Run: python scraper/indeed_login.py"}

    try:
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
            _pause(2.0, 3.5)
            _dismiss_popup(page)

            # Click Schedule Interview
            si_btn = page.get_by_test_id("prioritized-schedule-interview-button")
            if si_btn.count() == 0:
                browser.close()
                return {"ok": False, "error": "Schedule Interview button not found on profile page"}
            si_btn.click()
            _pause(2.0, 3.0)

            # Select specific time (vs share availability)
            try:
                st_opt = page.get_by_text("Select a specific time")
                if st_opt.count() > 0:
                    st_opt.click()
                    _pause(0.8, 1.2)
            except Exception:
                pass

            # Duration
            try:
                dur_btn = page.get_by_test_id(f"InterviewTimesSelector-duration-{duration}")
                if dur_btn.count() > 0:
                    dur_btn.click()
                    _pause(0.4, 0.7)
            except Exception:
                pass

            # Date — try text input first, then calendar picker
            try:
                dt = datetime.strptime(interview_date, "%Y-%m-%d")
                date_str_us = dt.strftime("%m/%d/%Y")

                date_input = page.get_by_test_id("InterviewDateTimeSelector-date-input-0")
                if date_input.count() > 0:
                    date_input.click()
                    date_input.fill(date_str_us)
                    page.keyboard.press("Tab")
                    _pause(0.5, 0.9)
                else:
                    cal_btn = page.get_by_role("button", name=re.compile(r"Choose a date"))
                    if cal_btn.count() > 0:
                        cal_btn.click()
                        _pause(0.8, 1.2)
                        # Try clicking the gridcell by day number
                        day = str(dt.day)
                        try:
                            page.get_by_role("gridcell", name=re.compile(rf"^{day}\b")).first.click()
                            _pause(0.5, 0.8)
                        except Exception:
                            pass
            except Exception as e:
                log.warning(f"Date selection warning (non-fatal): {e}")

            # Format: Phone / Video / In-person
            try:
                fmt_el = page.get_by_test_id("interview-details").get_by_text(format_, exact=True)
                if fmt_el.count() > 0:
                    fmt_el.click()
                    _pause(0.3, 0.6)
            except Exception:
                pass

            # Message to candidate
            try:
                msg_ta = page.get_by_test_id("gt-interview-form-message-to-candidate-text-area")
                if msg_ta.count() > 0:
                    msg_ta.click()
                    msg_ta.press("Control+A")
                    msg_ta.fill(message)
                    _pause(0.3, 0.6)
            except Exception:
                pass

            # Send interview request
            send_btn = page.get_by_role("button", name="Send interview request")
            if send_btn.count() == 0:
                browser.close()
                return {"ok": False, "error": "Send interview request button not found on form"}
            send_btn.click()
            _pause(3.0, 4.5)

            browser.close()

        candidate["status"] = "interviewing"
        candidate["indeed_message_sent"] = True
        candidate["indeed_message_sent_at"] = now_iso()
        candidate["interview"] = {
            "date": interview_date,
            "start_time": start_time,
            "duration_min": duration,
            "format": format_,
            "message": message,
            "scheduled_at": now_iso(),
        }
        save_json(path, candidate)

        log.info(f"Interview scheduled with {full_name} on {interview_date} {start_time}")
        return {"ok": True, "status": "interviewing"}

    except Exception as e:
        log.error(f"schedule_interview error: {e}")
        return {"ok": False, "error": str(e)}
