"""
End-to-end test: AI draft → post to Indeed (RMS account).
Step 1: draft job fields + JD from free text using Groq
Step 2: post to Indeed via Playwright

Usage:
    python scraper/test_post_job_e2e.py
"""
import json
import time
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, get_logger
from scraper.job_drafter import draft_from_text

log = get_logger("test_post_job_e2e")

FREE_TEXT = (
    "Looking for a full-time RN Case Manager in Lombard IL. "
    "Pay $40-$50 per hour. Need someone within 2 weeks. "
    "Must have active IL RN license and 2+ years home health experience. "
    "In-person role at our Lombard office."
)


def snap(page, name: str):
    out = Path("data/screenshots") / f"e2e_{name}.png"
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
    # Try clicking the first matching suggestion
    try:
        opts = page.get_by_role("option")
        opts.first.wait_for(timeout=6_000)
        # Prefer an option that contains the typed title
        for i in range(min(opts.count(), 5)):
            opt = opts.nth(i)
            txt = opt.text_content() or ""
            if any(w.lower() in txt.lower() for w in title.split()):
                opt.click()
                log.info(f"Selected title suggestion: {txt.strip()}")
                return
        opts.first.click()
        log.info(f"Selected first title suggestion")
    except Exception:
        combo.press("Tab")
        log.info(f"No suggestion found — typed: {title}")


def _select_location(page, query: str, pick: str):
    import re as _re
    loc = page.get_by_test_id("location-input-component")
    loc.click()
    loc.press("Control+A")
    loc.fill(query.lower())   # e.g. "lombard"
    _pause(2.5, 3.5)

    # codegen: page.locator("div").filter(has_text=re.compile(r"^Lombard, IL 60148$")).click()
    city = query.split(",")[0].strip()
    try:
        el = page.locator("div").filter(has_text=_re.compile(rf"^{_re.escape(pick)}$"))
        el.wait_for(timeout=5_000)
        el.click()
        log.info(f"Selected location div: {pick}")
        return
    except Exception:
        pass
    # fallback: any div containing city name
    try:
        el = page.locator("div").filter(has_text=_re.compile(rf"^{_re.escape(city)}")).first
        el.wait_for(timeout=3_000)
        el.click()
        log.info(f"Selected location fallback: {city}")
    except Exception:
        log.warning(f"Location dropdown not found — leaving as typed")


def post_to_indeed(job: dict):
    if not SESSION_STATE_FILE.exists():
        log.error("No RMS session. Run: python scraper/indeed_login.py")
        return

    log.info("Launching Playwright to post job on Indeed...")

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
        snap(page, "01_jobs_page")

        # ── 2. Open "Create new" submenu then click Job ───────────────────────
        page.locator("a").filter(has_text="Create newCreate new").click()
        _pause(0.8, 1.2)
        page.get_by_test_id("menu-link-CreateNewJob").click()
        _pause(1.0, 1.5)
        snap(page, "02_create_menu")

        # ── 3. Brand new post ──────────────────────────────────────────────────
        page.get_by_test_id("JOBPOSTING_STARTNEW").check()
        _pause(0.5, 0.8)
        page.get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)
        snap(page, "03_post_type")

        # ══════════════════════════════════════════════════════════════════════
        # PAGE: Job basics
        # ══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='job-title']", timeout=15_000)
        snap(page, "04_job_basics")

        _select_job_title(page, job["title"])
        _pause(0.5, 0.8)

        import re as _re2
        loc_type = job.get("location_type", "In person")
        if loc_type != "In person":
            page.get_by_test_id("job-location-type-selector").click()
            _pause(0.5, 0.8)
            keyword = "Hybrid" if "Hybrid" in loc_type else "remote"
            page.get_by_role("option").filter(
                has_text=_re2.compile(keyword, _re2.IGNORECASE)
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
        snap(page, "05b_after_continue")   # shows validation errors if any

        # ══════════════════════════════════════════════════════════════════════
        # PAGE: Hiring goals
        # ══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("[data-testid='expect-hire-date-input']", timeout=15_000)
        snap(page, "06_hiring_goals")

        # Hiring timeline — codegen: span "Select an option" opens dropdown
        _TIMELINE_MAP = {
            "1 to 3 days":       "to 7 days",
            "to 3 days":         "to 7 days",
            "1 to 2 weeks":      "to 2 weeks",
            "to 2 weeks":        "to 2 weeks",
            "2 to 4 weeks":      "to 4 weeks",
            "to 4 weeks":        "to 4 weeks",
            "More than 4 weeks": "More than 4 weeks",
        }
        raw_timeline = job.get("hiring_timeline", "to 4 weeks")
        indeed_timeline = _TIMELINE_MAP.get(raw_timeline, raw_timeline)

        try:
            page.locator("span").filter(has_text="Select an option").first.click()
            _pause(0.4, 0.7)
            page.get_by_text(indeed_timeline).first.click()
            log.info(f"Hiring timeline set: {indeed_timeline}")
        except Exception as e:
            log.warning(f"Timeline dropdown failed: {e}")
        _pause(0.5, 0.8)

        # Hires count — fill directly (codegen: .fill("3"))
        needed = int(job.get("hires_needed", 1))
        try:
            inp = page.get_by_test_id("job-hires-needed-input")
            inp.click()
            _pause(0.3, 0.5)
            inp.fill(str(needed))
            log.info(f"Hires needed set to: {needed}")
        except Exception as e:
            log.warning(f"Could not set hires count: {e}")

        snap(page, "07_hiring_goals_filled")
        # Try footer-large first, fall back to any continue button
        try:
            page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        except Exception:
            page.get_by_test_id("footer-continue-btn").first.click()
        _pause(2.5, 3.5)
        snap(page, "07b_after_continue")

        # ══════════════════════════════════════════════════════════════════════
        # PAGE: Job details (job type)
        # ══════════════════════════════════════════════════════════════════════
        page.get_by_role("heading", name="Add job details").wait_for(timeout=15_000)
        snap(page, "08_job_details")

        for jtype in job.get("job_types", ["Full-time"]):
            page.locator("label").filter(has_text=jtype).click()
            _pause(0.3, 0.5)

        snap(page, "09_job_type_selected")
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ══════════════════════════════════════════════════════════════════════
        # PAGE: Pay and benefits
        # ══════════════════════════════════════════════════════════════════════
        page.get_by_role("heading", name="Add pay and benefits").wait_for(timeout=15_000)
        snap(page, "10_pay_benefits")

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

        # Benefits
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
                log.warning(f"Could not select benefit: {benefit}")

        snap(page, "12_benefits")
        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(1.5, 2.5)

        # ══════════════════════════════════════════════════════════════════════
        # PAGE: Job description
        # ══════════════════════════════════════════════════════════════════════
        page.get_by_role("heading", name="Describe the job").wait_for(timeout=15_000)
        snap(page, "13_description_page")

        jd = page.get_by_role("textbox", name="Job description")
        jd.click()
        jd.press("Control+A")
        jd.fill(job["description"])
        _pause(0.8, 1.2)
        snap(page, "14_description_filled")

        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(2.0, 3.0)

        # ══════════════════════════════════════════════════════════════════════
        # PAGE: Review
        # ══════════════════════════════════════════════════════════════════════
        page.wait_for_selector("h1, h2", timeout=15_000)
        snap(page, "15_review")
        log.info("Review page loaded — confirming post...")

        page.get_by_test_id("footer-large").get_by_test_id("footer-continue-btn").click()
        _pause(2.0, 3.0)
        snap(page, "16_confirmed")

        # Skip sponsored upsell
        try:
            no_thanks = page.get_by_role("button", name="No thanks")
            if no_thanks.count() > 0:
                no_thanks.first.click()
                _pause(1.0, 1.5)
                log.info("Skipped sponsored upsell")
        except Exception:
            pass

        snap(page, "17_done")
        log.info("Job posted successfully on Indeed!")
        browser.close()


def run():
    # ── Step 1: AI draft ──────────────────────────────────────────────────────
    log.info("Step 1: Drafting job from free text...")
    log.info(f"Text: {FREE_TEXT}\n")

    job = draft_from_text(FREE_TEXT)

    if not job.get("ok"):
        log.error(f"Draft failed: {job.get('error')}")
        return

    if job.get("missing"):
        log.error(f"Missing required fields: {job['missing']} — cannot post.")
        return

    log.info(f"Title:     {job['title']}")
    log.info(f"Location:  {job['location_query']} / {job['location_type']}")
    log.info(f"Pay:       ${job.get('pay_min')} – ${job.get('pay_max')} {job.get('pay_period')}")
    log.info(f"Timeline:  {job['hiring_timeline']}")
    log.info(f"JD:        {job['description'][:200]}...\n")

    # Save for reference
    out = Path("data/screenshots") / "e2e_drafted_job.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Saved draft: {out}")

    # ── Step 2: Post to Indeed ────────────────────────────────────────────────
    log.info("\nStep 2: Posting to Indeed via Playwright...")
    post_to_indeed(job)


if __name__ == "__main__":
    run()
