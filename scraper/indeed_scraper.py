"""
Indeed employer scraper — pulls new SLP applicants and saves them as JSON.

Requires config/session_state.json to exist. Run scraper/indeed_login.py first.

Usage:
    python scraper/indeed_scraper.py
    python scraper/indeed_scraper.py --visible    # show browser window
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, Page, BrowserContext

from scraper.utils import (
    SESSION_STATE_FILE,
    CANDIDATES_DIR,
    JOBS_URL,
    get_logger,
    load_credentials,
    load_last_run,
    save_last_run,
    load_candidates_index,
    save_candidates_index,
    append_audit_log,
    save_json,
    now_iso,
)

log = get_logger("indeed_scraper")

EMPLOYER_BASE = "https://employers.indeed.com"


def is_session_valid(page: Page) -> bool:
    url = page.url
    return (
        "employers.indeed.com" in url
        and "/login" not in url
        and "/signin" not in url
        and "challenge" not in url
        and "secure.indeed.com" not in url
    )


def get_job_ids(page: Page, creds: dict) -> list[str]:
    """Return list of job IDs from the jobs listing page."""
    job_posting_id = creds.get("job_posting_id", "").strip()
    if job_posting_id:
        log.info(f"Using configured job ID: {job_posting_id}")
        return [job_posting_id]

    log.info("No job_posting_id in credentials — scanning jobs page for open postings...")
    job_ids: list[str] = []

    try:
        page.wait_for_selector("[data-testid='job-card'], .jobCard, [class*='JobCard']", timeout=15_000)
        job_cards = page.locator("[data-testid='job-card'], .jobCard, [class*='JobCard']").all()

        for card in job_cards:
            href = card.get_attribute("href") or ""
            link = card.locator("a").first
            if link.count() > 0:
                href = link.get_attribute("href") or ""
            if "/jobs/" in href:
                jid = href.split("/jobs/")[1].split("/")[0].split("?")[0]
                if jid and jid not in job_ids:
                    job_ids.append(jid)
    except Exception as e:
        log.warning(f"Could not auto-detect job IDs: {e}")

    log.info(f"Found {len(job_ids)} job posting(s): {job_ids}")
    return job_ids


def get_applicant_cards(page: Page, job_id: str) -> list[dict]:
    """Navigate to a job's candidates page and return basic applicant info."""
    candidates_url = f"{EMPLOYER_BASE}/jobs/{job_id}/candidates?status=new"
    log.info(f"Loading candidates for job {job_id}...")
    page.goto(candidates_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)

    if not is_session_valid(page):
        log.error("Session expired while loading candidates page.")
        return []

    applicants: list[dict] = []

    try:
        page.wait_for_selector(
            "[data-testid='applicant-card'], [class*='ApplicantCard'], [class*='applicant-card'], [class*='CandidateCard']",
            timeout=12_000,
        )
    except Exception:
        log.info("No applicant cards found (may be no new applicants).")
        return []

    cards = page.locator(
        "[data-testid='applicant-card'], [class*='ApplicantCard'], [class*='applicant-card'], [class*='CandidateCard']"
    ).all()

    log.info(f"Found {len(cards)} applicant card(s)")

    for card in cards:
        try:
            # Extract applicant ID from card link
            link_el = card.locator("a").first
            href = ""
            if link_el.count() > 0:
                href = link_el.get_attribute("href") or ""

            applicant_id = ""
            if "/candidates/" in href:
                applicant_id = href.split("/candidates/")[1].split("?")[0].split("/")[0]

            # Fallback: try data attributes
            if not applicant_id:
                applicant_id = (
                    card.get_attribute("data-applicant-id")
                    or card.get_attribute("data-id")
                    or ""
                )

            if not applicant_id:
                log.debug("Could not extract applicant ID from card — skipping")
                continue

            name = ""
            name_el = card.locator(
                "[data-testid='applicant-name'], [class*='applicantName'], [class*='ApplicantName'], h2, h3"
            ).first
            if name_el.count() > 0:
                name = name_el.inner_text().strip()

            full_url = href if href.startswith("http") else f"{EMPLOYER_BASE}{href}"

            applicants.append({
                "applicant_id": applicant_id,
                "full_name": name,
                "profile_url": full_url,
                "job_id": job_id,
            })
        except Exception as e:
            log.debug(f"Error parsing card: {e}")

    return applicants


def scrape_applicant_profile(page: Page, applicant: dict) -> dict:
    """Open an applicant's profile page and extract all available data."""
    profile_url = applicant["profile_url"]
    log.info(f"Scraping profile: {applicant['full_name']} — {profile_url}")

    page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)

    data: dict = {
        "id": f"indeed-{applicant['applicant_id']}",
        "source": "indeed",
        "scraped_at": now_iso(),
        "full_name": applicant["full_name"],
        "phone": None,
        "email": None,
        "indeed_profile_url": profile_url,
        "resume_text": "",
        "license_state": None,
        "license_status": None,
        "credential": None,
        "population": [],
        "setting_pref": [],
        "geography": None,
        "travel_radius_mi": None,
        "availability": None,
        "start_date": None,
        "pay_expectation": None,
        "work_auth": None,
        "status": "new",
        "fit_score": None,
        "flags": [],
        "pay_band_verdict": None,
        "decline_reason": None,
        "indeed_message_sent": False,
        "indeed_message_sent_at": None,
        "interview": None,
        "offer_outcome": None,
        "notes": "",
    }

    try:
        # Name (may be more accurate on profile page)
        name_el = page.locator("[data-testid='applicant-name'], h1, [class*='applicantName']").first
        if name_el.count() > 0:
            full_name = name_el.inner_text().strip()
            if full_name:
                data["full_name"] = full_name

        # Email
        email_el = page.locator("a[href^='mailto:']").first
        if email_el.count() > 0:
            href = email_el.get_attribute("href") or ""
            data["email"] = href.replace("mailto:", "").strip()

        # Phone
        phone_el = page.locator("a[href^='tel:']").first
        if phone_el.count() > 0:
            href = phone_el.get_attribute("href") or ""
            data["phone"] = href.replace("tel:", "").strip()

        # Location / geography
        location_el = page.locator(
            "[data-testid='applicant-location'], [class*='location'], [class*='Location']"
        ).first
        if location_el.count() > 0:
            data["geography"] = location_el.inner_text().strip()

        # Resume tab — try to click it and extract text
        resume_tab = page.locator("button:has-text('Resume'), a:has-text('Resume'), [data-testid='resume-tab']").first
        if resume_tab.count() > 0:
            resume_tab.click()
            page.wait_for_timeout(1500)

        resume_el = page.locator(
            "[data-testid='resume-text'], [class*='resumeText'], [class*='ResumeText'], [class*='resume-content']"
        ).first
        if resume_el.count() > 0:
            data["resume_text"] = resume_el.inner_text().strip()

        if not data["resume_text"]:
            # Fallback: grab all visible text from main content area
            content_el = page.locator("main, [role='main'], [class*='content']").first
            if content_el.count() > 0:
                data["resume_text"] = content_el.inner_text().strip()[:8000]

    except Exception as e:
        log.warning(f"Partial profile extraction for {data['full_name']}: {e}")

    return data


def main():
    parser = argparse.ArgumentParser(description="Scrape new SLP applicants from Indeed employer dashboard")
    parser.add_argument("--visible", action="store_true", help="Show browser window (useful for debugging)")
    args = parser.parse_args()

    if not SESSION_STATE_FILE.exists():
        log.error(f"No session file found at {SESSION_STATE_FILE}")
        log.error("Run the login script first:  python scraper/indeed_login.py")
        sys.exit(1)

    try:
        creds = load_credentials()
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    last_run = load_last_run()
    seen_ids: set[str] = set(last_run.get("applicants_seen", []))
    candidates_index = load_candidates_index()

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    new_count = 0
    all_new_applicant_ids: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.visible,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context: BrowserContext = browser.new_context(
            storage_state=str(SESSION_STATE_FILE),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        log.info(f"Loading jobs page: {JOBS_URL}")
        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(2000)

        if not is_session_valid(page):
            log.error(f"Session is invalid or expired. Current URL: {page.url}")
            log.error("Re-run the login script:  python scraper/indeed_login.py")
            browser.close()
            sys.exit(1)

        log.info(f"Session valid. On: {page.url}")

        job_ids = get_job_ids(page, creds)
        if not job_ids:
            log.error("No job IDs found. Set 'job_posting_id' in config/credentials.json.")
            browser.close()
            sys.exit(1)

        for job_id in job_ids:
            applicant_cards = get_applicant_cards(page, job_id)

            for card in applicant_cards:
                indeed_id = f"indeed-{card['applicant_id']}"

                if indeed_id in seen_ids:
                    log.debug(f"Already seen: {indeed_id} — skipping")
                    continue

                profile_data = scrape_applicant_profile(page, card)

                candidate_file = CANDIDATES_DIR / f"{indeed_id}.json"
                save_json(candidate_file, profile_data)
                log.info(f"Saved: {candidate_file.name}")

                index_entry = {
                    "id": indeed_id,
                    "full_name": profile_data["full_name"],
                    "source": "indeed",
                    "scraped_at": profile_data["scraped_at"],
                    "status": "new",
                    "fit_score": None,
                }
                candidates_index.append(index_entry)

                append_audit_log({
                    "event": "applicant_scraped",
                    "applicant_id": indeed_id,
                    "full_name": profile_data["full_name"],
                    "job_id": job_id,
                })

                all_new_applicant_ids.append(indeed_id)
                seen_ids.add(indeed_id)
                new_count += 1

        browser.close()

    # Persist updated state
    last_run["last_run_at"] = now_iso()
    last_run["applicants_seen"] = list(seen_ids)
    save_last_run(last_run)
    save_candidates_index(candidates_index)

    log.info(f"Done. {new_count} new applicant(s) saved.")
    if new_count == 0:
        log.info("No new applicants found since last run.")


if __name__ == "__main__":
    main()
