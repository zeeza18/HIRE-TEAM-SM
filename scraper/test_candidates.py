"""
Test ALL jobs — navigate to each job and list all candidates with status.
Run: python scraper/test_candidates.py
"""

import re
import sys
import time
import random
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, JOBS_URL, get_logger

log = get_logger("test_candidates")

VALID_STATUSES = {"New", "Reviewing", "Contacting", "Interviewing",
                  "Rejected", "Hired", "Invited", "Pending"}
HEADER_NAMES = {"Candidates", "Name", "Status"}


def _pause(lo: float, hi: float):
    """Random human-paced sleep between lo and hi seconds."""
    delay = random.uniform(lo, hi)
    log.debug(f"  ⏱  sleeping {delay:.1f}s")
    time.sleep(delay)


def dismiss_popup(page):
    popup = page.locator("[data-testid='onboarding-popup-close']")
    if popup.count() > 0:
        popup.click()
        page.wait_for_timeout(300)


def get_candidates_for_job(page, employer_job_id, title):
    """Navigate to job candidates page and return all candidates across all pages."""
    # Go back to jobs list and click this job's link
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)
    page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)
    page.wait_for_timeout(500)

    job_link = page.locator(
        f"[data-testid='UnifiedJobTldLink'][href*='{employer_job_id[:30]}']"
    ).first
    if job_link.count() == 0:
        log.warning(f"  Link not found for {title!r}")
        return []

    job_link.click()
    _pause(2.5, 4.5)          # human read time on job view page
    dismiss_popup(page)

    # Click "All Applications received" summary panel
    try:
        summary = page.locator("div").filter(
            has_text=re.compile(r"AllApplications receivedView all applications")
        ).nth(2)
        if summary.count() > 0:
            summary.click()
            _pause(1.0, 2.0)
    except Exception:
        pass

    # Click "All" candidates filter link
    all_btn = page.locator("[data-testid='candidates-pipeline-hosted-all-link']").first
    if all_btn.count() > 0:
        all_btn.click()
        _pause(1.5, 3.0)      # wait for table to render

    dismiss_popup(page)

    # Wait for candidate table
    try:
        page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=15_000)
    except Exception:
        log.info(f"  No candidates table — skipping")
        return []

    all_candidates: list[dict] = []
    page_num = 1

    while True:
        rows = page.locator(
            "[data-testid='candidate-list-table-container'] [role='row']"
        ).all()
        data_rows = [
            r for r in rows
            if r.locator("[role='checkbox'], input[type='checkbox']").count() > 0
            or r.locator("td").count() > 0
        ]

        page_count = 0
        for row in data_rows:
            lines = [l.strip() for l in row.inner_text().strip().split("\n") if l.strip()]
            if not lines or lines[0] in HEADER_NAMES:
                continue
            name = lines[0]
            status = next((l for l in lines if l in VALID_STATUSES), "?")
            all_candidates.append({"name": name, "status": status})
            page_count += 1

        log.info(f"    Page {page_num}: {page_count} candidate(s)")

        next_btn = page.locator("button:has-text('Next'), a:has-text('Next')").last
        if next_btn.count() > 0 and next_btn.is_enabled():
            next_btn.click()
            page_num += 1
            _pause(1.8, 3.2)  # between pagination pages
        else:
            break

    return all_candidates


with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(storage_state=str(SESSION_STATE_FILE))
    page = context.new_page()

    # ── Step 1: Collect all jobs ───────────────────────────────────────────────
    log.info("Loading jobs page to collect all jobs...")
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)
    page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)

    # Scroll until no new rows appear
    prev = 0
    for _ in range(10):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        count = page.locator("[data-testid='UnifiedJobTldLink']").count()
        if count == prev:
            break
        prev = count

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    for link in page.locator("[data-testid='UnifiedJobTldLink']").all():
        href = link.get_attribute("href") or ""
        title = link.inner_text().strip()
        if "employerJobId=" not in href:
            continue
        params = parse_qs(urlparse(href).query)
        eid = params.get("employerJobId", [""])[0]
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            jobs.append({"title": title, "employer_job_id": eid})

    log.info(f"Found {len(jobs)} job(s):")
    for j in jobs:
        log.info(f"  • {j['title']}")

    # ── Step 2: Process each job ───────────────────────────────────────────────
    all_results: dict[str, list[dict]] = {}
    grand_total = 0

    for idx, job in enumerate(jobs, 1):
        log.info(f"\n[{idx}/{len(jobs)}] {job['title']}")
        candidates = get_candidates_for_job(page, job["employer_job_id"], job["title"])
        all_results[job["title"]] = candidates
        grand_total += len(candidates)

        status_counts = Counter(c["status"] for c in candidates)
        log.info(f"  Subtotal: {len(candidates)}")
        for s, n in sorted(status_counts.items()):
            log.info(f"    {s:15} {n}")

        # Cooldown between jobs so Indeed doesn't rate-limit us
        if idx < len(jobs):
            _pause(4.0, 7.0)

    # ── Step 3: Grand summary ──────────────────────────────────────────────────
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  GRAND TOTAL: {grand_total} candidates across {len(jobs)} job(s)")
    print(sep)

    for job_title, candidates in all_results.items():
        status_counts = Counter(c["status"] for c in candidates)
        print(f"\n  {job_title}  ({len(candidates)} total)")
        for s, n in sorted(status_counts.items()):
            print(f"    {s:15} {n}")

        # List every candidate
        print()
        for i, c in enumerate(candidates, 1):
            print(f"    {i:3}. [{c['status']:12}] {c['name']}")

    print(f"\n{sep}")
    input("\nPress Enter to close...")
    browser.close()
