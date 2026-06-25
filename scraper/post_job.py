"""
Playwright job poster for Indeed (RMS account).
Called by the Flask API with a job JSON file.

Usage:
    python scraper/post_job.py --job data/_pending_job.json
"""
import argparse
import json
import re as _re
import sys
import time
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, get_logger

log = get_logger("post_job")


def snap(page, name: str):
    out = Path("data/screenshots") / f"post_{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))
    log.info(f"Screenshot: {out}")


def _pause(lo=0.8, hi=1.6):
    time.sleep(random.uniform(lo, hi))


def _select_job_title(page, title: str):
    combo = page.get_by_role("combobox", name="Job title")
    combo.click()
    combo.fill(title)
    _pause(2.0, 3.0)
    try:
        opts = page.get_by_role("option")
        opts.first.wait_for(timeout=6_000)
        for i in range(min(opts.count(), 5)):
            txt = opts.nth(i).text_content() or ""
            if any(w.lower() in txt.lower() for w in title.split()):
                opts.nth(i).click()
                log.info(f"Selected title: {txt.strip()}")
                return
        opts.first.click()
    except Exception:
        combo.press("Tab")
        log.info(f"Typed title: {title}")


def _select_location(page, query: str, pick: str):
    loc = page.get_by_test_id("location-input-component")
    loc.click()
    loc.press("Control+A")
    loc.fill(query.lower())
    _pause(2.5, 3.5)
    city = query.split(",")[0].strip()
    try:
        el = page.locator("div").filter(has_text=_re.compile(rf"^{_re.escape(pick)}$"))
        el.wait_for(timeout=5_000)
        el.click()
        log.info(f"Selected location: {pick}")
        return
    except Exception:
        pass
    try:
        el = page.locator("div").filter(has_text=_re.compile(rf"^{_re.escape(city)}")).first
        el.wait_for(timeout=3_000)
        el.click()
        log.info(f"Selected location (fallback): {city}")
    except Exception:
        log.warning(f"Location not found in dropdown")


_TIMELINE_MAP = {
    "1 to 3 days":       "to 7 days",
    "to 3 days":         "to 7 days",
    "1 to 2 weeks":      "to 2 weeks",
    "to 2 weeks":        "to 2 weeks",
    "2 to 4 weeks":      "to 4 weeks",
    "to 4 weeks":        "to 4 weeks",
    "More than 4 weeks": "More than 4 weeks",
}


def post_to_indeed(job: dict):
    if not SESSION_STATE_FILE.exists():
        log.error("No RMS session. Run: python scraper/indeed_login.py")
        return False

    log.info(f"Posting: {job.get('title')} in {job.get('location_query')}")

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

        # ── 1. Jobs page ───────────────────────────────────────────────────────
        page.goto(
            "https://employers.indeed.com/jobs"
            "?status=open%2Cpaused&claimed=false&createdOnIndeed=true"
            "&tab=0&sortDirection=DESC&sortField=datePostedOnIndeed",
            wait_until="domcontentloaded", timeout=30_000,
        )
        _pause(2.0, 3.0)
        snap(page, "01_start")

        # ── 2. Create new → Job ────────────────────────────────────────────────
        page.locator("a").filter(has_text="Create newCreate new").click()
        _pause(0.8, 1.2)
        page.get_by_test_id("menu-link-CreateNewJob").click()
        _pause(1.0, 1.5)
        snap(page, "02_create")

        # ── 3. Brand new post → Continue ──────────────────────────────────────
        page.get_by_test_id("JOBPOSTING_STARTNEW").check()
        _pause(0.5, 0.8)
        page.get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)
        snap(page, "03_new_post")

        # ══════════════════════════════════════════════════════════════════════
        # Job basics
        # ══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='job-title']", timeout=15_000)
        snap(page, "04_basics")

        _select_job_title(page, job["title"])
        _pause(0.5, 0.8)

        loc_type = job.get("location_type", "In person")
        if loc_type != "In person":
            # It's a dropdown — click to open, then pick option
            page.get_by_test_id("job-location-type-selector").click()
            _pause(0.5, 0.8)
            keyword = "Hybrid" if "Hybrid" in loc_type else "remote"
            page.get_by_role("option").filter(
                has_text=_re.compile(keyword, _re.IGNORECASE)
            ).first.click()
            log.info(f"Selected location type: {keyword}")
        else:
            log.info("Location type: In person (default)")
        _pause(0.5, 0.8)

        _select_location(
            page,
            job.get("location_query", "Lombard"),
            job.get("location_pick", "Lombard, IL 60148"),
        )
        _pause(0.8, 1.2)
        snap(page, "05_basics_filled")

        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(2.5, 3.5)
        snap(page, "05b_continue")

        # ══════════════════════════════════════════════════════════════════════
        # Hiring goals
        # ══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='expect-hire-date-input']", timeout=15_000)
        snap(page, "06_hiring")

        indeed_timeline = _TIMELINE_MAP.get(
            job.get("hiring_timeline", "to 4 weeks"), "to 4 weeks"
        )
        try:
            page.locator("span").filter(has_text="Select an option").first.click()
            _pause(0.4, 0.7)
            page.get_by_text(indeed_timeline).first.click()
            log.info(f"Timeline: {indeed_timeline}")
        except Exception as e:
            log.warning(f"Timeline failed: {e}")
        _pause(0.5, 0.8)

        try:
            inp = page.get_by_test_id("job-hires-needed-input")
            inp.click()
            _pause(0.3, 0.5)
            inp.fill(str(int(job.get("hires_needed", 1))))
            log.info(f"Hires: {job.get('hires_needed', 1)}")
        except Exception as e:
            log.warning(f"Hires count failed: {e}")

        snap(page, "07_hiring_filled")
        try:
            page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        except Exception:
            page.get_by_test_id("footer-continue-btn").first.click()
        _pause(2.0, 3.0)

        # ══════════════════════════════════════════════════════════════════════
        # Job details (job type)
        # ══════════════════════════════════════════════════════════════════════
        page.get_by_role("heading", name="Add job details").wait_for(timeout=15_000)
        snap(page, "08_job_details")

        for jtype in job.get("job_types", ["Full-time"]):
            page.locator("label").filter(has_text=jtype).click()
            _pause(0.3, 0.5)

        snap(page, "09_job_type")
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ══════════════════════════════════════════════════════════════════════
        # Pay and benefits
        # ══════════════════════════════════════════════════════════════════════
        page.get_by_role("heading", name="Add pay and benefits").wait_for(timeout=15_000)
        snap(page, "10_pay")

        if not job.get("pay_negotiable"):
            page.get_by_test_id("pay-type-selector").get_by_text("Range").click()
            _pause(0.4, 0.7)
            page.get_by_test_id("pay-period-selector").click()
            _pause(0.3, 0.5)
            page.get_by_text(job.get("pay_period", "per hour"), exact=True).click()
            _pause(0.3, 0.5)
            if job.get("pay_min"):
                b = page.get_by_role("textbox", name="Minimum")
                b.click(); b.press("Control+A"); b.fill(str(job["pay_min"]))
                _pause(0.3, 0.5)
            if job.get("pay_max"):
                b = page.get_by_role("textbox", name="Maximum")
                b.click(); b.press("Control+A"); b.fill(str(job["pay_max"]))
                _pause(0.3, 0.5)

        snap(page, "11_pay_filled")

        try:
            show_more = page.get_by_role("button").filter(has_text="more")
            if show_more.count() > 0:
                show_more.first.click()
                _pause(0.8, 1.2)
        except Exception:
            pass

        for benefit in job.get("benefits", []):
            try:
                lbl = page.locator("label").filter(has_text=benefit)
                if lbl.count() > 0:
                    lbl.first.click()
                    _pause(0.2, 0.4)
            except Exception:
                pass

        snap(page, "12_benefits")
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ══════════════════════════════════════════════════════════════════════
        # Job description
        # ══════════════════════════════════════════════════════════════════════
        page.get_by_role("heading", name="Describe the job").wait_for(timeout=15_000)
        snap(page, "13_description")

        jd = page.get_by_role("textbox", name="Job description")
        jd.click()
        jd.press("Control+A")
        jd.fill(job["description"])
        _pause(0.8, 1.2)
        snap(page, "14_description_filled")

        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(2.0, 3.0)

        # ══════════════════════════════════════════════════════════════════════
        # Review → Confirm
        # ══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("h1, h2", timeout=15_000)
        snap(page, "15_review")
        log.info("Review page — confirming...")

        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(2.0, 3.0)
        snap(page, "16_confirmed")

        try:
            no_thanks = page.get_by_role("button", name="No thanks")
            if no_thanks.count() > 0:
                no_thanks.first.click()
                _pause(1.0, 1.5)
                log.info("Skipped sponsored upsell")
        except Exception:
            pass

        snap(page, "17_done")
        log.info(f"Job posted: {job.get('title')}")
        browser.close()
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, help="Path to job JSON file")
    args = parser.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    success = post_to_indeed(job)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
