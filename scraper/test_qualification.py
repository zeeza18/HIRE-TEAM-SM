"""
Qualification extraction test.

For each candidate in each job:
  1. Click into their profile page
  2. Expand the Qualifications section
  3. Read every Required / Preferred item from summary-accordion
  4. Screenshot at every key step for debugging
  5. Return to list

Screenshots: data/screenshots/qual_debug/
Run:         python scraper/test_qualification.py
"""

import base64
import re
import sys
import time
import random
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, Page
from scraper.utils import SESSION_STATE_FILE, JOBS_URL, get_logger

log = get_logger("test_qualification")

VALID_STATUSES = {"New", "Reviewing", "Contacting", "Interviewing",
                  "Rejected", "Hired", "Invited", "Pending"}
HEADER_NAMES   = {"Candidates", "Name", "Status"}

SHOT_DIR = Path(__file__).parent.parent / "data" / "screenshots" / "qual_debug"
SHOT_DIR.mkdir(parents=True, exist_ok=True)
_shot_n = 0


def snap(page: Page, label: str):
    global _shot_n
    _shot_n += 1
    slug = re.sub(r"[^\w]+", "_", label)[:50]
    name = f"{_shot_n:04d}_{slug}.png"
    page.screenshot(path=str(SHOT_DIR / name), full_page=False)
    log.info(f"    snap: {name}")


def _eid_slug(eid: str) -> str:
    """Decode base64 eid and return the UUID tail — URL-safe, unique per job."""
    try:
        decoded = base64.b64decode(eid).decode("utf-8")
        return decoded.rstrip("/").split("/")[-1]
    except Exception:
        return eid


def _pause(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))


def _dismiss(page: Page):
    el = page.locator("[data-testid='onboarding-popup-close']")
    if el.count() > 0:
        el.click()
        page.wait_for_timeout(300)


# ── qualification reader (called from inside profile page) ────────────────────

_debug_dumped = False
_nonmet_dumped = False

# SVG CSS classes from HTML dump (accordion_dump.html):
#   css-1xcc4qr = green checkmark  → met
#   css-zzdap5  = non-green icon   → not_met (X) or unknown (?)
# Checkmark SVG path starts with "M10.582" — used to confirm met via path data.
# X-icon and ?-icon have different paths — logged on first occurrence to accordion_nonmet_dump.html.

def read_qualifications(page: Page, cand_name: str) -> list[dict]:
    """
    Returns list of {"label", "section", "match"} dicts.
    match: "met" | "not_met" | "unknown"
    """
    global _debug_dumped, _nonmet_dumped

    # Expand if collapsed
    try:
        btn = page.get_by_role("button", name="Qualifications").first
        if btn.count() > 0 and btn.get_attribute("aria-expanded") != "true":
            btn.click()
            page.wait_for_timeout(900)
    except Exception:
        pass

    snap(page, f"qual_open_{cand_name}")

    accordion = page.locator("[data-testid='summary-accordion']").first
    if accordion.count() == 0:
        snap(page, f"NO_ACCORDION_{cand_name}")
        log.warning(f"    summary-accordion not found for {cand_name!r}")
        return []

    if not _debug_dumped:
        try:
            (SHOT_DIR / "accordion_dump.html").write_text(
                accordion.inner_html(), encoding="utf-8"
            )
            log.info("    HTML dump -> accordion_dump.html")
        except Exception:
            pass
        _debug_dumped = True

    # ── Step 1: ordered (section, label) from summary-header data-testid ─────
    header_chips = accordion.locator(
        "[data-testid^='Required:'], [data-testid^='Preferred:']"
    ).all()

    label_section: list[tuple[str, str]] = []
    for chip in header_chips:
        tid = chip.get_attribute("data-testid") or ""
        if ":" not in tid:
            continue
        sec, lbl = tid.split(":", 1)
        label_section.append((lbl.strip(), sec.strip()))

    # ── Step 2: ordered match status from body chips (div.css-1het13u) ───────
    body_chips = accordion.locator("div.css-1het13u").all()

    def _chip_match(chip) -> str:
        svg = chip.locator("svg").first
        if svg.count() == 0:
            return "unknown"
        cls = svg.get_attribute("class") or ""

        if "css-1xcc4qr" in cls:
            return "met"

        if "css-zzdap5" in cls:
            # Dump path data for first non-met icon to identify X vs ? paths
            global _nonmet_dumped
            if not _nonmet_dumped:
                try:
                    info = page.evaluate(
                        """(el) => {
                            const paths = Array.from(el.querySelectorAll('path'));
                            return {
                                cls: el.className,
                                paths: paths.map(p => p.getAttribute('d') || '').join(' || '),
                                color: window.getComputedStyle(el).color,
                                parentColor: window.getComputedStyle(el.parentElement).color
                            };
                        }""",
                        svg.element_handle()
                    )
                    log.info(f"    [nonmet debug] {info}")
                    (SHOT_DIR / "accordion_nonmet_dump.html").write_text(
                        accordion.inner_html(), encoding="utf-8"
                    )
                    log.info("    nonmet HTML dump -> accordion_nonmet_dump.html")
                except Exception as e:
                    log.debug(f"    nonmet dump failed: {e}")
                _nonmet_dumped = True

            # Distinguish X (not_met) from ? (unknown) via SVG path data
            # Checkmark: path starts "M10.582"
            # X/close icon: path typically contains diagonal line coords like "L17.59"
            # ? icon: different curved path
            try:
                path_d = svg.locator("path").first.get_attribute("d") or ""
                # X/close icons have symmetric diagonal strokes → look for characteristic coords
                if re.search(r"L\s*17\.5|6\.41|M19\s+6|M4\.7", path_d):
                    return "not_met"
            except Exception:
                pass
            return "unknown"

        return "unknown"

    match_statuses = [_chip_match(c) for c in body_chips]

    # ── Step 3: zip label+section with match status ───────────────────────────
    quals: list[dict] = []
    for i, (label, section) in enumerate(label_section):
        match = match_statuses[i] if i < len(match_statuses) else "unknown"
        quals.append({"label": label, "section": section, "match": match})

    # Fallback: if header chips not found, parse inner_text
    if not quals:
        current = "Required"
        skip = {"qualifications", "required", "preferred"}
        for line in accordion.inner_text().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.lower() in skip:
                if line.lower() in ("required", "preferred"):
                    current = line
                continue
            quals.append({"label": line, "section": current, "match": "unknown"})

    if not quals:
        snap(page, f"NO_QUALS_{cand_name}")
        log.warning(f"    No quals for {cand_name!r}")

    for q in quals:
        sym = {"met": "v", "not_met": "X", "unknown": "?"}[q["match"]]
        log.debug(f"      {sym} [{q['section']}] {q['label']}")

    return quals


# ── navigate to candidate list ────────────────────────────────────────────────

def get_jobs(page: Page) -> list[dict]:
    log.info("Loading jobs page...")
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(2000)
    page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=20_000)

    prev = 0
    for _ in range(10):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        n = page.locator("[data-testid='UnifiedJobTldLink']").count()
        if n == prev:
            break
        prev = n

    jobs, seen = [], set()
    for link in page.locator("[data-testid='UnifiedJobTldLink']").all():
        href  = link.get_attribute("href") or ""
        title = link.inner_text().strip()
        if "employerJobId=" not in href:
            continue
        eid = parse_qs(urlparse(href).query).get("employerJobId", [""])[0]
        if eid and eid not in seen:
            seen.add(eid)
            jobs.append({"title": title, "employer_job_id": eid})
            log.info(f"  • {title}")

    log.info(f"Found {len(jobs)} job(s)")
    return jobs


def go_to_all_candidates(page: Page, job: dict) -> bool:
    eid, title = job["employer_job_id"], job["title"]

    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)
    page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)

    # Match by parsed employerJobId — handles URL-encoding and base64 safely
    clicked_link = None
    for lnk in page.locator("[data-testid='UnifiedJobTldLink']").all():
        href = lnk.get_attribute("href") or ""
        params = parse_qs(urlparse(href).query)
        if params.get("employerJobId", [""])[0] == eid:
            clicked_link = lnk
            break
    if clicked_link is None:
        log.warning(f"Link not found for {title!r}")
        return False

    clicked_link.click()
    _pause(2.5, 4.0)
    _dismiss(page)

    _all_clicked = False
    for locator in [
        page.get_by_role("link",   name=re.compile(r"^All applications", re.I)).first,
        page.get_by_role("button", name=re.compile(r"^All applications", re.I)).first,
        page.locator("a, button").filter(has_text=re.compile(r"^All applications", re.I)).first,
    ]:
        try:
            if locator.count() > 0:
                locator.click()
                _pause(1.5, 2.5)
                _all_clicked = True
                log.info(f"  Clicked 'All applications' tab")
                break
        except Exception:
            pass

    if not _all_clicked:
        log.info(f"  'All applications' tab not found — using current view")

    _dismiss(page)

    try:
        page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=15_000)
        snap(page, f"list_{title}")
        return True
    except Exception:
        log.warning(f"No candidates table for {title!r}")
        return False


# ── process one candidate (click in → read quals → back) ─────────────────────

def process_candidate(page: Page, row, cand_name: str, list_url: str) -> list[dict]:
    try:
        row.locator("td").first.click()
        _pause(2.0, 3.5)
        _dismiss(page)
        snap(page, f"profile_{cand_name}")

        quals = read_qualifications(page, cand_name)

        # back to list
        try:
            back = page.locator("[data-testid='BackToListButton']").first
            if back.count() > 0:
                back.click()
            else:
                page.go_back()
            _pause(1.5, 2.5)
            _dismiss(page)
            page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=12_000)
        except Exception as back_err:
            log.debug(f"  back-nav error [{cand_name}]: {back_err}")
            page.goto(list_url, wait_until="domcontentloaded", timeout=20_000)
            _pause(1.5, 2.5)
            page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=12_000)

        return quals

    except Exception as e:
        log.warning(f"  process error [{cand_name}]: {e}")
        snap(page, f"error_{cand_name}")
        try:
            page.goto(list_url, wait_until="domcontentloaded", timeout=20_000)
            _pause(1.5, 2.5)
            page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=12_000)
        except Exception:
            pass
        return []


# ── main ──────────────────────────────────────────────────────────────────────

import argparse as _argparse
_ap = _argparse.ArgumentParser()
_ap.add_argument("--limit", type=int, default=0, help="Max candidates per job (0 = all)")
_args = _ap.parse_args()
PER_JOB_LIMIT = _args.limit or 0

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(storage_state=str(SESSION_STATE_FILE))
    page    = context.new_page()

    jobs = get_jobs(page)
    all_results: dict[str, list[dict]] = {}

    for job_idx, job in enumerate(jobs, 1):
        title = job["title"]
        log.info(f"\n[{job_idx}/{len(jobs)}] {title}")

        if not go_to_all_candidates(page, job):
            continue

        list_url   = page.url
        page_num   = 1
        job_output: list[dict] = []

        while True:
            rows = page.locator(
                "[data-testid='candidate-list-table-container'] [role='row']"
            ).all()
            page_candidates: list[tuple[str, str]] = []
            for row in rows:
                lines = [l.strip() for l in row.inner_text().strip().split("\n") if l.strip()]
                if not lines or lines[0] in HEADER_NAMES:
                    continue
                if not row.locator("td").count():
                    continue
                name   = lines[0]
                status = next((l for l in lines if l in VALID_STATUSES), "?")
                page_candidates.append((name, status))

            log.info(f"  Page {page_num}: {len(page_candidates)} candidate(s)")

            for cand_name, cand_status in page_candidates:
                if PER_JOB_LIMIT and len(job_output) >= PER_JOB_LIMIT:
                    break
                # re-query to avoid stale handles after navigation
                live_rows = page.locator(
                    "[data-testid='candidate-list-table-container'] [role='row']"
                ).all()
                matched_row = None
                for row in live_rows:
                    lines = [l.strip() for l in row.inner_text().strip().split("\n") if l.strip()]
                    if lines and lines[0] == cand_name:
                        matched_row = row
                        break

                if matched_row is None:
                    log.warning(f"  Could not re-find row for {cand_name!r}")
                    continue

                quals = process_candidate(page, matched_row, cand_name, list_url)
                record = {"name": cand_name, "status": cand_status, "quals": quals}
                job_output.append(record)
                sym = {"met": "v", "not_met": "X", "unknown": "?"}
                met_n   = sum(1 for q in quals if q["match"] == "met")
                total_n = len(quals)
                detail  = ", ".join(f"{sym.get(q['match'],'?')} {q['label']}" for q in quals)
                log.info(
                    f"  + {cand_name} [{cand_status}] "
                    f"{met_n}/{total_n} -> {detail}"
                )
                _pause(0.6, 1.2)

            if PER_JOB_LIMIT and len(job_output) >= PER_JOB_LIMIT:
                break
            next_btn = page.locator("button:has-text('Next'), a:has-text('Next')").last
            if next_btn.count() > 0 and next_btn.is_enabled():
                next_btn.click()
                page_num += 1
                _pause(1.8, 3.2)
            else:
                break

        all_results[title] = job_output
        if job_idx < len(jobs):
            _pause(4.0, 7.0)

    # ── summary ───────────────────────────────────────────────────────────────
    sep = "=" * 72
    print(f"\n{sep}")
    print("  QUALIFICATION MATCH SUMMARY")
    print(sep)

    for job_title, candidates in all_results.items():
        print(f"\n  {job_title}  ({len(candidates)} candidate(s))")
        print(f"  {'-' * 62}")
        for c in candidates:
            met_n   = sum(1 for q in c["quals"] if q["match"] == "met")
            total_n = len(c["quals"])
            score   = f"{met_n}/{total_n}" if total_n else "0/0"
            sym = {"met": "v", "not_met": "X", "unknown": "?"}
            q_str = (
                ", ".join(f"{sym.get(q['match'],'?')} {q['label']}" for q in c["quals"])
                if c["quals"] else "(none captured)"
            )
            print(f"    [{c['status']:12}] {score}  {c['name']}")
            print(f"                      {q_str}")

    print(f"\n{sep}")
    print(f"\n  Screenshots → {SHOT_DIR}")
    input("\nPress Enter to close...")
    browser.close()
