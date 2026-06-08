"""
Full profile scraper — ALL active jobs (open + paused, no cancelled/stopped).

For each job → each candidate:
  - Clicks into profile
  - Extracts name, location, email, phone, qualifications, summary,
    experience, certifications, education, skills, resume text, cover letter
  - Downloads resume PDF via JS blob fetch
  - Saves JSON to data/candidates/  +  PDF to data/resumes/
  - Uses BackToListButton to keep pagination state

Run: python scraper/test_profile_scrape.py
"""

import base64
import hashlib
import re
import sys
import time
import random
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import (
    SESSION_STATE_FILE, JOBS_URL, CANDIDATES_DIR,
    get_logger, save_json, now_iso,
)

log = get_logger("profile_scrape")

RESUMES_DIR = Path(__file__).parent.parent / "data" / "resumes"
DEBUG_DIR   = Path(__file__).parent.parent / "data"

VALID_STATUSES = {"New", "Reviewing", "Contacting", "Interviewing",
                  "Rejected", "Hired", "Invited", "Pending"}
HEADER_NAMES   = {"Candidates", "Name", "Status"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _pause(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))

def _dismiss_popup(page):
    try:
        p = page.locator("[data-testid='onboarding-popup-close']")
        if p.count() > 0:
            p.click()
            page.wait_for_timeout(300)
    except Exception:
        pass

def _page_alive(page) -> bool:
    try:
        _ = page.url
        return True
    except Exception:
        return False

def _screenshot(page, label: str):
    try:
        page.screenshot(path=str(DEBUG_DIR / f"debug_{label}.png"), full_page=False)
        log.info(f"    Screenshot → debug_{label}.png")
    except Exception:
        pass

def _expand(page, button_name: str):
    """Expand accordion only if currently collapsed."""
    try:
        btn = page.get_by_role("button", name=button_name).first
        if btn.count() == 0:
            return
        if btn.get_attribute("aria-expanded") != "true":
            btn.click()
            page.wait_for_timeout(700)
    except Exception:
        pass

def _read(page, selector: str) -> str:
    try:
        el = page.locator(selector).first
        if el.count() > 0:
            return el.inner_text().strip()
    except Exception:
        pass
    return ""

def _get_id(page, fallback_name: str) -> str:
    try:
        parsed = urlparse(page.url)
        params = parse_qs(parsed.query)
        for key in ("applicantId", "id", "candidateId", "jobseekerExternalId"):
            val = params.get(key, [""])[0]
            if val and val not in ("view", "candidates"):
                return val
        skip = {"candidates", "view", "jobs", "hire", ""}
        parts = [s for s in parsed.path.split("/") if s not in skip]
        if parts:
            return parts[-1]
    except Exception:
        pass
    return hashlib.md5(fallback_name.encode()).hexdigest()[:12]

def _download_blob(page, dl_href: str, save_path: Path) -> bool:
    try:
        b64 = page.evaluate("""async (url) => {
            const resp = await fetch(url);
            const blob = await resp.blob();
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                reader.onerror   = reject;
                reader.readAsDataURL(blob);
            });
        }""", dl_href)
        if b64:
            save_path.write_bytes(base64.b64decode(b64))
            return True
    except Exception as e:
        log.warning(f"    Blob fetch failed: {e}")
    return False


# ── profile extractor ─────────────────────────────────────────────────────────

def scrape_profile(page, row_name: str, row_status: str,
                   job_id: str, job_title: str) -> dict:
    applicant_id = _get_id(page, row_name)
    indeed_id    = f"indeed-{applicant_id}"

    data: dict = {
        "id":                   indeed_id,
        "source":               "indeed",
        "scraped_at":           now_iso(),
        "full_name":            row_name,
        "status":               row_status.lower(),
        "job_id":               job_id,
        "job_title":            job_title,
        "indeed_profile_url":   page.url,
        "phone":                None,
        "email":                None,
        "geography":            None,
        "qualifications":       "",
        "professional_summary": "",
        "experience":           "",
        "certifications":       "",
        "education":            "",
        "skills":               "",
        "resume_text":          "",
        "cover_letter":         "",
        "resume_file":          None,
        "fit_score":            None,
        "flags":                [],
        "notes":                "",
    }

    try:
        # Name
        name_el = page.locator("[data-testid='name-plate-name-item']").first
        if name_el.count() > 0:
            n = name_el.inner_text().strip()
            if n:
                data["full_name"] = n

        # Contact
        email_el = page.locator("a[href^='mailto:']").first
        if email_el.count() > 0:
            data["email"] = (email_el.get_attribute("href") or "").replace("mailto:", "").strip()
        phone_el = page.locator("a[href^='tel:']").first
        if phone_el.count() > 0:
            data["phone"] = (phone_el.get_attribute("href") or "").replace("tel:", "").strip()

        # Location
        loc_el = page.locator("[data-testid='name-plate-location-item']").first
        if loc_el.count() > 0:
            data["geography"] = loc_el.inner_text().strip()

        # Accordions
        _expand(page, "Qualifications")
        data["qualifications"] = _read(page, "[data-testid='summary-accordion']")

        _expand(page, "Professional summary")
        data["professional_summary"] = _read(page, "[data-testid='profile-section-Professional summary']")

        _expand(page, "Experience")
        exp_parts = [el.inner_text().strip()
                     for el in page.locator("[data-testid^='experienceSection-']").all()
                     if el.inner_text().strip()]
        data["experience"] = "\n\n".join(exp_parts)[:6000]

        _expand(page, "Certifications & licenses")
        data["certifications"] = _read(page, "[data-testid='profile-section-Certifications & licenses']")

        _expand(page, "Education")
        data["education"] = _read(page, "[data-testid='profile-section-Education']")

        _expand(page, "Skills")
        data["skills"] = _read(page, "[data-testid='profile-section-Skills']")

        # Resume
        resume_btn = page.get_by_role("button", name="Resume", exact=True).first
        if resume_btn.count() > 0:
            if resume_btn.get_attribute("aria-expanded") != "true":
                resume_btn.click()
                _pause(1.0, 1.8)

            pdf_el = page.locator("[data-testid='pdf-resume-view']").first
            if pdf_el.count() > 0:
                data["resume_text"] = pdf_el.inner_text().strip()[:8000]

            dl_btn = page.locator("[data-testid='download-resume-inline']").first
            if dl_btn.count() > 0:
                dl_href = dl_btn.get_attribute("href") or ""
                if dl_href:
                    save_path = RESUMES_DIR / f"{indeed_id}.pdf"
                    if _download_blob(page, dl_href, save_path):
                        data["resume_file"] = str(save_path)
                        log.info(f"    Resume → {save_path.name}")
                    else:
                        try:
                            with page.expect_download(timeout=10_000) as dl_info:
                                dl_btn.dispatch_event("click")
                            dl_info.value.save_as(str(save_path))
                            data["resume_file"] = str(save_path)
                            log.info(f"    Resume (fallback) → {save_path.name}")
                        except Exception as e2:
                            log.warning(f"    Resume skipped: {e2}")
                            _screenshot(page, f"resume_fail_{indeed_id[:16]}")

        # Cover letter
        cover_el = page.locator("[data-testid='cover-letter']").first
        if cover_el.count() > 0:
            data["cover_letter"] = cover_el.inner_text().strip()[:3000]

    except Exception as e:
        log.warning(f"  Partial extraction [{data['full_name']}]: {e}")
        _screenshot(page, f"extract_fail_{indeed_id[:16]}")

    return data


# ── navigation helpers ────────────────────────────────────────────────────────

def _recover(page, context, list_url: str):
    """Return a live page pointing at the candidates list."""
    if not _page_alive(page):
        log.warning("  Page dead — reopening...")
        page = context.new_page()
    page.goto(list_url, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)
    _dismiss_popup(page)
    return page

def _go_all_candidates(page, job: dict) -> str | None:
    """
    From the jobs page, click job title → All candidates link.
    Returns the candidates-list URL on success, None on failure.
    """
    title          = job["title"]
    employer_job_id = job["employer_job_id"]

    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)
    page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)

    job_link = page.locator(
        f"[data-testid='UnifiedJobTldLink'][href*='{employer_job_id[:30]}']"
    ).first
    if job_link.count() == 0:
        log.warning(f"  Link not found for {title!r}")
        return None

    job_link.click()
    _pause(2.5, 4.5)
    _dismiss_popup(page)

    # "All Applications received" summary panel
    try:
        summary = page.locator("div").filter(
            has_text=re.compile(r"AllApplications receivedView all applications")
        ).nth(2)
        if summary.count() > 0:
            summary.click()
            _pause(1.0, 2.0)
    except Exception:
        pass

    all_btn = page.locator("[data-testid='candidates-pipeline-hosted-all-link']").first
    if all_btn.count() > 0:
        all_btn.click()
        _pause(1.5, 3.0)
    _dismiss_popup(page)

    try:
        page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=15_000)
    except Exception:
        log.warning(f"  No candidates table for {title!r}")
        _screenshot(page, f"no_list_{title[:16]}")
        return None

    return page.url


# ── main ──────────────────────────────────────────────────────────────────────

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(storage_state=str(SESSION_STATE_FILE))
    page    = context.new_page()

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: collect all open/paused jobs (cancelled already excluded by URL)
    log.info("Loading jobs page...")
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)
    page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)

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
        href  = link.get_attribute("href") or ""
        title = link.inner_text().strip()
        if "employerJobId=" not in href:
            continue
        params = parse_qs(urlparse(href).query)
        eid    = params.get("employerJobId", [""])[0]
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            jobs.append({"title": title, "employer_job_id": eid})

    log.info(f"Found {len(jobs)} active job(s):")
    for j in jobs:
        log.info(f"  • {j['title']}")

    # ── Step 2: scrape each job ────────────────────────────────────────────────
    grand_saved   = 0
    grand_skipped = 0
    job_summaries: list[dict] = []

    for job_idx, job in enumerate(jobs, 1):
        title = job["title"]
        eid   = job["employer_job_id"]
        log.info(f"\n{'='*62}")
        log.info(f"[{job_idx}/{len(jobs)}] {title}")
        log.info(f"{'='*62}")

        list_url = _go_all_candidates(page, job)
        if list_url is None:
            job_summaries.append({"title": title, "saved": 0, "skipped": 0, "error": True})
            continue

        saved_this_job   = 0
        skipped_this_job = 0
        page_num         = 1

        while True:
            if not _page_alive(page):
                page = _recover(page, context, list_url)
                page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=15_000)

            rows = page.locator(
                "[data-testid='candidate-list-table-container'] [role='row']"
            ).all()
            data_rows = [
                r for r in rows
                if r.locator("[role='checkbox'], input[type='checkbox']").count() > 0
                or r.locator("td").count() > 0
            ]

            page_candidates: list[tuple[str, str]] = []
            for row in data_rows:
                lines = [l.strip() for l in row.inner_text().strip().split("\n") if l.strip()]
                if not lines or lines[0] in HEADER_NAMES:
                    continue
                name   = lines[0]
                status = next((l for l in lines if l in VALID_STATUSES), "new")
                page_candidates.append((name, status))

            log.info(f"  Page {page_num}: {len(page_candidates)} candidate(s)")

            for cand_name, cand_status in page_candidates:
                log.info(f"    → {cand_name} [{cand_status}]")

                safe = cand_name.lower().replace(" ", "_")
                if list(CANDIDATES_DIR.glob(f"*{safe[:20]}*.json")):
                    log.info("       Already saved — skipping")
                    skipped_this_job += 1
                    continue

                try:
                    # Re-query rows and click this candidate
                    rows = page.locator(
                        "[data-testid='candidate-list-table-container'] [role='row']"
                    ).all()
                    clicked = False
                    for row in rows:
                        lines = [l.strip() for l in row.inner_text().strip().split("\n") if l.strip()]
                        if not lines or lines[0] in HEADER_NAMES or lines[0] != cand_name:
                            continue
                        row.locator("td").first.click()
                        _pause(2.0, 3.5)
                        clicked = True
                        break

                    if not clicked:
                        log.warning(f"       Row not found — skipping {cand_name!r}")
                        _screenshot(page, f"miss_{safe[:14]}")
                        continue

                    candidate_data = scrape_profile(page, cand_name, cand_status, eid, title)

                    out_path = CANDIDATES_DIR / f"{candidate_data['id']}.json"
                    save_json(out_path, candidate_data)
                    log.info(f"       Saved → {out_path.name}")
                    saved_this_job += 1

                    # Return to list
                    if not _page_alive(page):
                        raise RuntimeError("page died during scrape")

                    back = page.locator("[data-testid='BackToListButton']").first
                    if back.count() > 0:
                        back.click()
                    else:
                        page.go_back()
                    _pause(1.5, 2.5)
                    _dismiss_popup(page)
                    page.wait_for_selector(
                        "[data-testid='candidate-list-table-container']", timeout=10_000
                    )

                except Exception as e:
                    log.warning(f"       Error [{cand_name}]: {e}")
                    _screenshot(page, f"err_{safe[:14]}")
                    # Recovery
                    if not _page_alive(page):
                        page = _recover(page, context, list_url)
                    else:
                        try:
                            back = page.locator("[data-testid='BackToListButton']").first
                            if back.count() > 0:
                                back.click()
                            else:
                                page.go_back()
                            _pause(2.0, 3.0)
                            _dismiss_popup(page)
                            page.wait_for_selector(
                                "[data-testid='candidate-list-table-container']", timeout=10_000
                            )
                        except Exception:
                            page = _recover(page, context, list_url)
                            page.wait_for_selector(
                                "[data-testid='candidate-list-table-container']", timeout=15_000
                            )

            # Pagination
            next_btn = page.locator("button:has-text('Next'), a:has-text('Next')").last
            if next_btn.count() > 0 and next_btn.is_enabled():
                next_btn.click()
                page_num += 1
                _pause(2.0, 3.5)
            else:
                break

        grand_saved   += saved_this_job
        grand_skipped += skipped_this_job
        job_summaries.append({
            "title":   title,
            "saved":   saved_this_job,
            "skipped": skipped_this_job,
            "error":   False,
        })
        log.info(f"  Job done: {saved_this_job} saved, {skipped_this_job} skipped")

        # Cooldown between jobs
        if job_idx < len(jobs):
            log.info("  Cooling down before next job...")
            _pause(5.0, 9.0)

    # ── Final summary ──────────────────────────────────────────────────────────
    sep = "=" * 62
    print(f"\n{sep}")
    print(f"  ALL JOBS COMPLETE")
    print(f"  Total saved : {grand_saved}")
    print(f"  Total skipped: {grand_skipped}")
    print(sep)
    for js in job_summaries:
        status = "ERROR" if js["error"] else f"{js['saved']} saved  {js['skipped']} skipped"
        print(f"  {js['title']:40}  {status}")
    print(sep)

    input("\nPress Enter to close...")
    browser.close()
