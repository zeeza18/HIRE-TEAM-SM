"""
Post a job on Indeed (RMS account).
Can be run standalone (uses hardcoded defaults) or driven by the Flask API
via --job <path-to-json>.

Usage:
    python scraper/test_post_job.py                  # hardcoded defaults
    python scraper/test_post_job.py --job /path.json  # AI-drafted job
"""
import argparse
import json
import time
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, get_logger

log = get_logger("test_post_job")

# ── Hardcoded fallback (used when no --job arg) ───────────────────────────────
_DEFAULT_JOB = {
    "title":           "Intake Coordinator",
    "location_type":   "In person",
    "location_query":  "Lombard",
    "location_pick":   "Lombard, IL 60148",
    "hiring_timeline": "1 to 2 weeks",
    "hires_needed":    1,
    "job_types":       ["Full-time"],
    "pay_min":         "20",
    "pay_max":         "25",
    "pay_period":      "per hour",
    "pay_negotiable":  False,
    "benefits":        ["Health insurance", "Dental insurance", "Vision insurance"],
    "description": (
        "Position Overview:\n"
        "Reliable Medical Services, Inc. is seeking a detail-oriented and organized "
        "Intake Coordinator to manage the patient intake process for our home health agency.\n\n"
        "Key Responsibilities:\n"
        "• Receive, review, and process patient referrals from hospitals, physicians, and other sources\n"
        "• Verify patient insurance coverage and eligibility\n"
        "• Coordinate with clinical staff to ensure appropriate patient admissions\n"
        "• Enter and maintain accurate patient information in the EMR system\n"
        "• Schedule initial patient visits and coordinate care start dates\n\n"
        "Qualifications:\n"
        "• High school diploma or equivalent required\n"
        "• Minimum 1 year of healthcare intake experience\n"
        "• Knowledge of Medicare/Medicaid\n"
        "• Proficiency with EMR systems and Microsoft Office"
    ),
}


def _load_job() -> dict:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--job", default=None)
    args, _ = parser.parse_known_args()
    if args.job:
        p = Path(args.job)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        log.warning(f"--job file not found: {args.job}, using defaults")
    return _DEFAULT_JOB


JOB = _load_job()


def snap(page, name: str):
    out = Path("data/screenshots") / f"post_job_{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))
    log.info(f"Screenshot: {out}")


def _pause(lo=0.8, hi=1.6):
    time.sleep(random.uniform(lo, hi))


def _select_job_title(page, title: str):
    combo = page.get_by_role("combobox", name="Job title")
    combo.click()
    combo.fill(title)
    _pause(1.2, 2.0)
    # Try clicking the first matching suggestion
    try:
        suggestion = page.get_by_role("option").first
        suggestion.wait_for(timeout=5_000)
        suggestion.click()
        log.info(f"Selected job title suggestion for: {title}")
    except Exception:
        # No autocomplete — press Tab to accept typed value
        combo.press("Tab")
        log.info(f"No suggestion found, typed title: {title}")


def _select_location(page, query: str, pick: str):
    loc = page.get_by_test_id("location-input-component")
    loc.click()
    loc.fill(query)
    _pause(1.2, 2.0)
    try:
        page.get_by_text(pick, exact=True).wait_for(timeout=6_000)
        page.get_by_text(pick, exact=True).click()
        log.info(f"Selected location: {pick}")
    except Exception:
        loc.press("Tab")
        log.info(f"Location suggestion not found, accepted typed: {query}")


def run():
    if not SESSION_STATE_FILE.exists():
        log.error("No RMS session found. Run: python scraper/indeed_login.py")
        return

    log.info("Starting Indeed job post test (RMS account)...")

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

        # ── 1. Navigate to employer jobs page ─────────────────────────────────
        page.goto(
            "https://employers.indeed.com/jobs"
            "?status=open%2Cpaused&claimed=false&createdOnIndeed=true"
            "&tab=0&sortDirection=DESC&sortField=datePostedOnIndeed",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        _pause(2.0, 3.0)
        snap(page, "01_jobs_page")

        # ── 2. Open "Create new" menu → Job ───────────────────────────────────
        page.get_by_test_id("menu-link-CreateNewJob").click()
        _pause(1.0, 1.5)
        snap(page, "02_create_menu")

        # ── 3. Choose "Create a brand new post" ───────────────────────────────
        page.get_by_test_id("JOBPOSTING_STARTNEW").check()
        _pause(0.5, 0.8)
        page.get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)
        snap(page, "03_post_type_selected")

        # ═══════════════════════════════════════════════════════════════════════
        # PAGE: Add job basics
        # ═══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='job-title']", timeout=15_000)
        snap(page, "04_job_basics_loaded")

        # Job title
        _select_job_title(page, JOB["title"])
        _pause(0.5, 0.8)
        snap(page, "05_title_filled")

        # Location type
        loc_selector = page.get_by_test_id("job-location-type-selector")
        loc_selector.get_by_text(JOB["location_type"]).click()
        _pause(0.5, 0.8)

        # Location address
        _select_location(page, JOB["location_query"], JOB["location_pick"])
        _pause(0.8, 1.2)
        snap(page, "06_location_filled")

        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ═══════════════════════════════════════════════════════════════════════
        # PAGE: Hiring goals
        # ═══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='expect-hire-date-input']", timeout=15_000)
        snap(page, "07_hiring_goals_loaded")

        # Hiring timeline
        page.get_by_test_id("expect-hire-date-input").click()
        _pause(0.4, 0.7)
        page.get_by_role("option", name=JOB["hiring_timeline"]).click()
        _pause(0.5, 0.8)

        # Number of hires — default is 1, adjust with +/- buttons
        current = int(page.get_by_test_id("job-hires-needed-input").input_value() or "1")
        diff = JOB["hires_needed"] - current
        btn = "hire-plus-btn" if diff > 0 else "hire-minus-btn"
        for _ in range(abs(diff)):
            page.get_by_test_id(btn).click()
            _pause(0.2, 0.4)

        snap(page, "08_hiring_goals_filled")
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ═══════════════════════════════════════════════════════════════════════
        # PAGE: Add job details (job type)
        # ═══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='job-type']", timeout=15_000)
        snap(page, "09_job_details_loaded")

        for jtype in JOB["job_types"]:
            page.locator("label").filter(has_text=jtype).click()
            _pause(0.3, 0.5)

        snap(page, "10_job_type_selected")
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ═══════════════════════════════════════════════════════════════════════
        # PAGE: Add pay and benefits
        # ═══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='pay-type-selector']", timeout=15_000)
        snap(page, "11_pay_benefits_loaded")

        # Pay type = Range
        page.get_by_test_id("pay-type-selector").get_by_text("Range").click()
        _pause(0.4, 0.7)

        # Pay period
        page.get_by_test_id("pay-period-selector").click()
        _pause(0.3, 0.5)
        page.get_by_text(JOB["pay_period"], exact=True).click()
        _pause(0.3, 0.5)

        # Min pay
        min_box = page.get_by_role("textbox", name="Minimum")
        min_box.click()
        min_box.press("Control+A")
        min_box.fill(JOB["pay_min"])
        _pause(0.3, 0.5)

        # Max pay
        max_box = page.get_by_role("textbox", name="Maximum")
        max_box.click()
        max_box.press("Control+A")
        max_box.fill(JOB["pay_max"])
        _pause(0.3, 0.5)
        snap(page, "12_pay_filled")

        # Benefits — expand full list first
        try:
            show_more = page.get_by_role("button").filter(has_text="more")
            if show_more.count() > 0:
                show_more.first.click()
                _pause(0.8, 1.2)
        except Exception:
            pass

        for benefit in JOB["benefits"]:
            try:
                lbl = page.locator("label").filter(has_text=benefit)
                if lbl.count() > 0:
                    lbl.first.click()
                    _pause(0.2, 0.4)
                    log.info(f"Selected benefit: {benefit}")
            except Exception:
                log.warning(f"Could not select benefit: {benefit}")

        snap(page, "13_benefits_selected")
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ═══════════════════════════════════════════════════════════════════════
        # PAGE: Describe the job
        # ═══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[role='textbox'][name='Job description']", timeout=15_000)
        snap(page, "14_description_loaded")

        jd_box = page.get_by_role("textbox", name="Job description")
        jd_box.click()
        jd_box.press("Control+A")
        jd_box.fill(JOB["description"])
        _pause(0.8, 1.2)
        snap(page, "15_description_filled")

        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(2.0, 3.0)

        # ═══════════════════════════════════════════════════════════════════════
        # PAGE: Review
        # ═══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("h1, h2", timeout=15_000)
        snap(page, "16_review_page")
        log.info("Review page loaded — inspect before confirming.")

        # Confirm post
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(2.0, 3.0)
        snap(page, "17_post_confirmed")

        # Skip sponsored plan upsell
        try:
            no_thanks = page.get_by_role("button", name="No thanks")
            if no_thanks.count() > 0:
                no_thanks.first.click()
                _pause(1.0, 1.5)
                log.info("Skipped sponsored plan upsell")
        except Exception:
            pass

        snap(page, "18_final")
        log.info("Job post test complete. Check screenshots in data/screenshots/")

        browser.close()


if __name__ == "__main__":
    run()
