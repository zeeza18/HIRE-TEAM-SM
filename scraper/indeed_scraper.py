"""
Indeed employer scraper — all active jobs, all candidates, full profiles + PDF resumes.

Requires config/session_state.json. Run scraper/indeed_login.py first.

Usage:
    python scraper/indeed_scraper.py
    python scraper/indeed_scraper.py --visible
"""

import argparse
import base64
import hashlib
import json
import re
import sys
import threading
import time
import random
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    import pdfplumber
    _PDFPLUMBER = True
except ImportError:
    _PDFPLUMBER = False

try:
    from playwright_stealth.stealth import Stealth as _Stealth
    _STEALTH = True
except ImportError:
    _STEALTH = False

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    _TESSERACT = True
except ImportError:
    _TESSERACT = False

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright, Page, BrowserContext

from scraper.utils import (
    SESSION_STATE_FILE, CANDIDATES_DIR, JOBS_URL, CONFIG_DIR,
    get_logger, load_credentials, load_last_run, save_last_run,
    load_candidates_index, save_candidates_index,
    append_audit_log, save_json, load_json, now_iso,
    load_status_counts, save_status_counts,
)

log = get_logger("indeed_scraper")

RESUMES_DIR    = CANDIDATES_DIR.parent / "resumes"
JD_FILE        = CONFIG_DIR / "job_descriptions.json"
VALID_STATUSES = {"New", "Reviewing", "Contacting", "Interviewing",
                  "Rejected", "Hired", "Invited", "Pending", "Withdrawn"}
HEADER_NAMES   = {"Candidates", "Name", "Status"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _eid_slug(eid: str) -> str:
    """Decode base64 eid and return the UUID tail — URL-safe, unique per job."""
    try:
        decoded = base64.b64decode(eid).decode("utf-8")
        return decoded.rstrip("/").split("/")[-1]
    except Exception:
        return eid


def _pause(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))


def _debug_verify_saved(out_path: Path, label: str):
    """Re-stat the file we just wrote so disappearing-candidate reports can be
    root-caused from the log alone: absolute path actually used + on-disk proof.
    """
    try:
        resolved = out_path.resolve()
        ok = resolved.exists()
        size = resolved.stat().st_size if ok else -1
        log.info(f"  [debug-verify] {label}: {resolved} exists={ok} size={size} CANDIDATES_DIR={CANDIDATES_DIR.resolve()}")
    except Exception as e:
        log.warning(f"  [debug-verify] {label}: FAILED to verify {out_path}: {e}")

def _dismiss_popup(page: Page):
    try:
        p = page.locator("[data-testid='onboarding-popup-close']")
        if p.count() > 0:
            p.click()
            page.wait_for_timeout(300)
    except Exception:
        pass

def _page_alive(page: Page) -> bool:
    try:
        _ = page.url
        return True
    except Exception:
        return False

def _expand(page: Page, button_name: str):
    try:
        btn = page.get_by_role("button", name=button_name).first
        if btn.count() == 0:
            return
        if btn.get_attribute("aria-expanded") != "true":
            btn.click()
            page.wait_for_timeout(700)
    except Exception:
        pass

def _ask_timeout(prompt: str, timeout: float, default: str) -> str:
    """Prompt for yes/no; return default if no response within timeout seconds."""
    result = [default]
    answered = threading.Event()

    def _reader():
        try:
            result[0] = input(prompt)
        except (EOFError, KeyboardInterrupt):
            pass
        answered.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    if not answered.wait(timeout=timeout):
        mins = int(timeout / 60)
        print(f"\n  [timeout] No response in {mins}m — defaulting to '{default}'")
    return result[0]


_STATUS_TAB_NAMES: dict[str, str] = {
    "all":          "All applications",
    "new":          "New",
    "reviewing":    "Reviewing",
    "contacting":   "Contacting",
    "interviewing": "Interviewing",
    "rejected":     "Rejected",
    "hired":        "Hired",
}


def _read_status_counts(page: Page) -> dict:
    """Read candidate counts from Indeed's status filter tab labels."""
    counts: dict[str, int] = {}
    try:
        for el in page.locator("label").all():
            raw = el.inner_text().strip()
            flat = re.sub(r"\s+", "", raw)
            for key, name in _STATUS_TAB_NAMES.items():
                name_flat = re.sub(r"\s+", "", name)
                if flat.lower().startswith(name_flat.lower()):
                    m = re.search(r"(\d+)$", flat)
                    if m:
                        counts[key] = int(m.group(1))
                    break
    except Exception as e:
        log.debug(f"Status count read failed: {e}")
    return counts


def _read_qualifications(page: Page) -> tuple[list[dict], str]:
    """
    Expand the Qualifications accordion and return:
      (qual_list, score_str)

    qual_list: [{"label": str, "section": "Required"|"Preferred",
                 "match": "met"|"not_met"|"unknown"}, ...]
    score_str: "N/total" where N = met count

    DOM facts (from accordion_dump.html inspection):
      - Header summary icons have data-testid="Required: <label>"
      - Body chips are div.css-1het13u; first child SVG class tells match state:
          css-1xcc4qr = green checkmark (met)
          css-zzdap5  = non-green icon  (not_met or unknown)
      - X-icon (not_met) SVG path contains diagonal-line coords; ? path does not
    """
    _expand(page, "Qualifications")

    # Wait for chips to render before reading
    try:
        page.wait_for_selector(
            "[data-testid='summary-accordion'] div.css-1het13u",
            timeout=6_000
        )
    except Exception:
        pass

    accordion = page.locator("[data-testid='summary-accordion']").first
    if accordion.count() == 0:
        return [], "0/0"

    # Step 1: ordered (section, label) from data-testid on summary header icons
    header_chips = accordion.locator(
        "[data-testid^='Required:'], [data-testid^='Preferred:']"
    ).all()
    label_section: list[tuple[str, str]] = []
    for chip in header_chips:
        tid = chip.get_attribute("data-testid") or ""
        if ":" in tid:
            sec, lbl = tid.split(":", 1)
            label_section.append((lbl.strip(), sec.strip()))

    # Step 2: ordered match status from body chips (div.css-1het13u)
    def _match_from_chip(chip) -> str:
        svg = chip.locator("svg").first
        if svg.count() == 0:
            return "unknown"
        cls = svg.get_attribute("class") or ""
        if "css-1xcc4qr" in cls:
            return "met"
        if "css-zzdap5" in cls:
            try:
                path_d = svg.locator("path").first.get_attribute("d") or ""
                # X/close icon has symmetric diagonal stroke coords not in ? path
                if re.search(r"L\s*17\.5|6\.41|M19\s+6|M4\.7", path_d):
                    return "not_met"
            except Exception:
                pass
            return "unknown"
        return "unknown"

    body_chips = accordion.locator("div.css-1het13u").all()
    match_statuses = [_match_from_chip(c) for c in body_chips]

    # Step 3: zip
    quals: list[dict] = []
    for i, (label, section) in enumerate(label_section):
        match = match_statuses[i] if i < len(match_statuses) else "unknown"
        quals.append({"label": label, "section": section, "match": match})

    # Fallback: plain text parse if header chips not found
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

    met   = sum(1 for q in quals if q["match"] == "met")
    total = len(quals)
    score = f"{met}/{total}"
    return quals, score


def _read(page: Page, selector: str) -> str:
    try:
        el = page.locator(selector).first
        if el.count() > 0:
            return el.inner_text().strip()
    except Exception:
        pass
    return ""

def _get_id(page: Page, fallback_name: str) -> str:
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

def _pdf_to_text(pdf_path: Path) -> str:
    """Extract plain text from a PDF using pdfplumber, then OCR if image-based."""
    if not pdf_path.exists():
        return ""

    # Pass 1: pdfplumber (fast, exact — works when PDF has a text layer)
    if _PDFPLUMBER:
        try:
            import pdfplumber
            pages = []
            with pdfplumber.open(str(pdf_path)) as pdf:
                for pg in pdf.pages:
                    t = pg.extract_text()
                    if t:
                        pages.append(t)
            if pages:
                return "\n".join(pages)[:8000]
        except Exception as e:
            log.debug(f"pdfplumber failed on {pdf_path.name}: {e}")

    # Pass 2: OCR via Tesseract (for scanned / image-based PDFs)
    if _TESSERACT:
        return _ocr_pdf(pdf_path)

    return ""


def _ocr_pdf(pdf_path: Path) -> str:
    """Render each PDF page to an image and run Tesseract OCR on it."""
    try:
        import pypdfium2 as pdfium
        import pytesseract
        from PIL import Image

        doc = pdfium.PdfDocument(str(pdf_path))
        pages_text = []
        for page in doc:
            # Render at 200 DPI (scale=200/72)
            bitmap = page.render(scale=200 / 72, rotation=0)
            pil_img = bitmap.to_pil()
            text = pytesseract.image_to_string(pil_img, lang="eng")
            if text.strip():
                pages_text.append(text.strip())
        doc.close()
        result = "\n".join(pages_text)
        log.debug(f"OCR extracted {len(result)} chars from {pdf_path.name}")
        return result[:8000]
    except Exception as e:
        log.debug(f"OCR failed on {pdf_path.name}: {e}")
        return ""


def _normalize_contact_text(txt: str) -> str:
    """Collapse PDF line-break artifacts that split phone numbers across lines."""
    # "(815) 557\n-\n3192" → "(815) 557-3192"
    txt = re.sub(r'(\d+)\n[-–]\n(\d+)', r'\1-\2', txt)
    # "\n(815)\n557-3192" → " (815) 557-3192"
    txt = re.sub(r'\n(\(\d{3}\))\n', r' \1 ', txt)
    return txt


def _download_blob(page: Page, dl_href: str, save_path: Path) -> bool:
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
        log.debug(f"Blob fetch failed: {e}")
    return False

def is_session_valid(page: Page) -> bool:
    url = page.url
    return (
        "employers.indeed.com" in url
        and "/login" not in url
        and "/signin" not in url
        and "challenge" not in url
        and "secure.indeed.com" not in url
    )


# ── job listing ───────────────────────────────────────────────────────────────

def get_jobs(page: Page) -> list[dict]:
    """Return all jobs. Tries live page first; falls back to stored job_descriptions.json."""
    log.info("Scanning jobs table...")
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)

    jobs: list[dict] = []
    seen_ids: set[str] = set()

    try:
        try:
            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=20_000, state="attached")
        except Exception:
            page.wait_for_load_state("networkidle", timeout=10_000)
            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000, state="attached")

        prev = 0
        for _ in range(10):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            count = page.locator("[data-testid='UnifiedJobTldLink']").count()
            if count == prev:
                break
            prev = count

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
                log.info(f"  - {title}")
    except Exception as e:
        log.warning(f"Job scan error: {e}")

    # Screenshot for debugging when live scan fails
    if not jobs:
        try:
            snap_dir = CANDIDATES_DIR.parent / "screenshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap_path = snap_dir / "debug_jobs_page.png"
            page.screenshot(path=str(snap_path), full_page=True)
            log.info(f"Debug screenshot saved: {snap_path}")
        except Exception as se:
            log.warning(f"Could not save debug screenshot: {se}")

    # Fallback: use job IDs already stored in job_descriptions.json
    if not jobs and JD_FILE.exists():
        try:
            stored = json.loads(JD_FILE.read_text(encoding="utf-8"))
            for eid, data in stored.items():
                if eid not in seen_ids:
                    title = data.get("title", "Unknown")
                    jobs.append({"title": title, "employer_job_id": eid})
                    seen_ids.add(eid)
                    log.info(f"  • {title} (from stored JD)")
            if jobs:
                log.info(f"Live scan found no jobs — using {len(jobs)} stored job(s) as fallback")
        except Exception as fe:
            log.warning(f"JD fallback failed: {fe}")

    log.info(f"Total active job(s): {len(jobs)}")
    return jobs


# ── navigate to All candidates for a job ─────────────────────────────────────

SCRAPE_TABS: list[tuple[str, str]] = [
    ("New",          "new"),
    ("Reviewing",    "reviewing"),
    ("Contacting",   "contacting"),
    ("Interviewing", "interviewing"),
    ("Rejected",     "rejected"),
    ("Hired",        "hired"),
]


def _click_tab(page: Page, tab_name: str, retries: int = 2) -> bool:
    """Click a status filter tab by name. Returns True if found and clicked."""
    # Indeed uses <label> radio elements for tabs.
    # Count the parent locator (not .first) to avoid Playwright's .first.count()==1 quirk.
    for attempt in range(retries):
        _dismiss_popup(page)
        try:
            page.keyboard.press("Home")
        except Exception:
            pass
        candidates = [
            page.locator("label").filter(has_text=tab_name),
            page.locator("label").filter(has_text=re.compile(rf"\b{re.escape(tab_name)}\b", re.I)),
            page.get_by_text(tab_name, exact=False),
        ]
        for loc in candidates:
            try:
                if loc.count() > 0:
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click()
                    _pause(1.5, 2.5)
                    return True
            except Exception:
                pass
        if attempt < retries - 1:
            log.debug(f"  '{tab_name}' tab not found on attempt {attempt + 1} — retrying")
            _pause(1.0, 2.0)
    return False


_LIST_CONTAINER_SEL = "[data-testid='candidate-list-table-container']"


def _find_row_by_name(page: Page, target_name: str, max_scrolls: int = 12):
    """Find a candidate row by name in a (possibly virtualized) list,
    scrolling the container incrementally until it renders. Returns the row
    locator, or None if not found after scrolling through the whole list."""
    container = page.locator(_LIST_CONTAINER_SEL).first
    try:
        container.evaluate("el => el.scrollTo(0, 0)")
    except Exception:
        pass

    for _ in range(max_scrolls):
        rows = page.locator(f"{_LIST_CONTAINER_SEL} [role='row']").all()
        for row in rows:
            try:
                lines = [l.strip() for l in row.inner_text().strip().split("\n") if l.strip()]
            except Exception:
                continue
            if lines and lines[0] == target_name:
                return row
        try:
            scrolled = container.evaluate(
                "el => { const before = el.scrollTop; "
                "el.scrollBy(0, el.clientHeight * 0.8); "
                "return el.scrollTop > before; }"
            )
        except Exception:
            break
        page.wait_for_timeout(400)
        if not scrolled:
            break
    return None


def go_all_candidates(page: Page, job: dict) -> tuple[str, dict] | None:
    """Navigate to the job's candidates page.
    Returns (base_url, status_counts) or None on failure."""
    title = job["title"]
    eid   = job["employer_job_id"]

    # Navigate directly to the candidates page using the employer job ID.
    # Avoids re-visiting JOBS_URL which triggers bot detection on repeated visits.
    candidates_url = f"https://employers.indeed.com/candidates?employerJobId={eid}"
    page.goto(candidates_url, wait_until="domcontentloaded", timeout=30_000)
    _pause(2.5, 4.5)
    _dismiss_popup(page)

    if not is_session_valid(page):
        log.error("Session expired mid-run.")
        return None

    # ── Ensure we're on the candidates list ──────────────────────────────────
    try:
        page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=8_000)
    except Exception:
        # May have landed on job view — try sidebar nav to candidates section
        for nav_sel in [
            "[data-testid='candidates-pipeline-hosted-all-link']",
            "[data-testid='menu-link-Candidates']",
            "a[href*='candidates']",
        ]:
            try:
                nav = page.locator(nav_sel).first
                if nav.count() > 0:
                    nav.click()
                    _pause(1.5, 2.5)
                    page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=20_000)
                    break
            except Exception:
                pass

    try:
        page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=8_000)
    except Exception:
        # Save a debug screenshot so we can see what's on screen
        try:
            snap_dir = CANDIDATES_DIR.parent / "screenshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(snap_dir / f"debug_candidates_{eid[:12]}.png"), full_page=True)
            log.info(f"  Debug screenshot saved for {title!r} (URL: {page.url})")
        except Exception:
            pass
        log.warning(f"No candidates table for {title!r}")
        return None

    # ── Read status counts from tab labels ────────────────────────────────────
    _pause(0.5, 1.0)   # let tab counts render
    counts = _read_status_counts(page)
    if counts:
        log.info("  Counts: " + "  ".join(
            f"{k}={v}" for k, v in counts.items() if v is not None
        ))

    return page.url, counts


# ── full profile scrape (on profile page) ────────────────────────────────────

def scrape_profile(page: Page, row_name: str, row_status: str,
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
        "requirements":         [],   # [{"label","section","match"}]
        "req_score":            "",   # "met/total" e.g. "3/5"
        "professional_summary": "",
        "experience":           "",
        "certifications":       "",
        "education":            "",
        "skills":               "",
        "resume_text":          "",
        "cover_letter":         "",
        "resume_file":          None,
        # Week 2 fields (Claude will populate)
        "license_state":        None,
        "license_status":       None,
        "credential":           None,
        "population":           [],
        "setting_pref":         [],
        "travel_radius_mi":     None,
        "availability":         None,
        "start_date":           None,
        "pay_expectation":      None,
        "work_auth":            None,
        "fit_score":            None,
        "flags":                [],
        "pay_band_verdict":     None,
        "decline_reason":       None,
        "indeed_message_sent":  False,
        "indeed_message_sent_at": None,
        "interview":            None,
        "offer_outcome":        None,
        "notes":                "",
    }

    try:
        name_el = page.locator("[data-testid='name-plate-name-item']").first
        if name_el.count() > 0:
            n = name_el.inner_text().strip()
            if n:
                data["full_name"] = n

        # Geography from profile plate (most reliable source)
        loc_el = page.locator("[data-testid='name-plate-location-item']").first
        if loc_el.count() > 0:
            data["geography"] = loc_el.inner_text().strip()

        data["requirements"], data["req_score"] = _read_qualifications(page)

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
                    else:
                        try:
                            with page.expect_download(timeout=10_000) as dl_info:
                                dl_btn.dispatch_event("click")
                            dl_info.value.save_as(str(save_path))
                            data["resume_file"] = str(save_path)
                        except Exception as e2:
                            log.debug(f"Resume download skipped: {e2}")

            # Fallback: pdfplumber then OCR if inline viewer returned nothing
            if not data["resume_text"] and data["resume_file"]:
                data["resume_text"] = _pdf_to_text(Path(data["resume_file"]))
                if data["resume_text"]:
                    log.debug(f"Recovered resume text via PDF fallback for {data['full_name']}")

        cover_el = page.locator("[data-testid='cover-letter']").first
        if cover_el.count() > 0:
            data["cover_letter"] = cover_el.inner_text().strip()[:3000]

        # ── Extract phone / email / location from resume text ─────────────
        txt = data["resume_text"] or data["cover_letter"]
        if txt:
            norm = _normalize_contact_text(txt)

            if not data["phone"]:
                m = re.search(
                    r'(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}', norm
                )
                if m:
                    data["phone"] = m.group().strip()

            if not data["email"]:
                m = re.search(r'[\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,}', norm)
                if m:
                    data["email"] = m.group().strip()

            if not data["geography"]:
                # Look for "City, ST 12345" or "City, ST" patterns
                m = re.search(
                    r'\b([A-Z][a-zA-Z .\']+,\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)',
                    norm
                )
                if m:
                    data["geography"] = m.group(1).strip()

    except Exception as e:
        log.warning(f"Partial extraction [{data['full_name']}]: {e}")

    return data


# ── JD pull ───────────────────────────────────────────────────────────────────

def _extract_jd_text(page: Page) -> str:
    """Extract JD from job detail page — tries multiple strategies for different layouts."""
    best = ""

    # Strategy 1a: semantic class (stable across CSS recompiles) — return immediately
    try:
        el = page.locator(".jd-appended-job-description").first
        if el.count() > 0:
            txt = el.inner_text().strip()
            if len(txt) > 100:
                return txt
    except Exception:
        pass

    # Strategy 1b: hashed CSS classes — pick longest
    for sel in [".css-1jpbxfu", ".css-19qk1my", ".css-15c5oio"]:
        try:
            for el in page.locator(sel).all():
                txt = el.inner_text().strip()
                if len(txt) > len(best):
                    best = txt
        except Exception:
            pass
    if len(best) > 100:
        return best

    # Strategy 2: walk up from "Job description:" label
    try:
        label = page.get_by_text("Job description:", exact=False).first
        if label.count() > 0:
            for depth in range(1, 6):
                js = (
                    f"(el) => {{ let n = el; "
                    f"for (let i=0;i<{depth};i++) {{ if(n.parentElement) n=n.parentElement; }} "
                    f"return n.innerText; }}"
                )
                txt = page.evaluate(js, label.element_handle())
                if txt and len(txt) > 200:
                    return txt.strip()
    except Exception:
        pass

    # Strategy 3: data-testid patterns
    for testid in ["job-description", "jobDescription", "job-details-description", "job-info-description"]:
        try:
            el = page.locator(f"[data-testid='{testid}']").first
            if el.count() > 0:
                txt = el.inner_text().strip()
                if len(txt) > 100:
                    return txt
        except Exception:
            pass

    # Strategy 4: largest div/section/article that contains "Job description"
    try:
        containers = page.locator("div, section, article").all()
        for c in containers:
            try:
                txt = c.inner_text()
                if "Job description" in txt and len(txt) > len(best):
                    best = txt
            except Exception:
                continue
        if len(best) > 200:
            return best.strip()
    except Exception:
        pass

    # Strategy 5: longest text block anywhere on the page (last resort)
    try:
        result = page.evaluate("""() => {
            let best = '';
            document.querySelectorAll('[class]').forEach(el => {
                const txt = (el.innerText || '').trim();
                if (txt.length > best.length && txt.length < 15000) best = txt;
            });
            return best;
        }""")
        if result and len(result) > 300:
            return result.strip()
    except Exception:
        pass

    return ""


def _extract_date_posted(body: str) -> str:
    """Pull the 'Date posted: <date>' line that Indeed always renders on the
    job detail page, right before Pay/Job description.
    """
    m = re.search(r"Date posted:\s*([A-Za-z]+ \d{1,2},?\s*\d{4})", body)
    return m.group(1).strip() if m else ""


def _extract_jd_from_body(body: str) -> str:
    """Extract JD from raw page body text using text markers — works when CSS selectors fail."""
    import re

    # Find 'Job description' header (Indeed always renders this label)
    for marker in ["Job description:\n", "Job description:", "Job Description:\n", "Job Description:"]:
        idx = body.find(marker)
        if idx != -1:
            text = body[idx + len(marker):]
            # Cut off at known footer boilerplate
            for cutoff in [
                "All analytics data provided",
                "Indeed reserves the right",
                "This information does not",
                "Report this job",
                "Save this job",
                "Apply now",
                "\nSign in\n",
            ]:
                ci = text.find(cutoff)
                if ci != -1:
                    text = text[:ci]
            text = text.strip()
            if len(text) > 100:
                return text

    # Fallback: find largest paragraph-style block (>200 chars, not UI chrome)
    blocks = re.split(r"\n{2,}", body)
    best = ""
    for block in blocks:
        block = block.strip()
        if len(block) > len(best) and len(block) < 8000 and "\n" in block:
            best = block
    return best if len(best) > 200 else ""


def _return_to_jobs_list(page: Page):
    """Get back to the jobs list with all links rendered, preferring
    page.go_back() over a fresh page.goto(JOBS_URL).

    A fresh top-level GET to the same URL right after a previous one is
    exactly the pattern that gets flagged: confirmed via screenshot during
    a real run that the *second* _load_jobs_list() reload in a row served
    Indeed's Cloudflare "Additional Verification Required" challenge instead
    of the jobs page. go_back() replays browser history instead of issuing a
    new request, so it doesn't trip the same heuristic.
    """
    try:
        page.go_back(wait_until="domcontentloaded", timeout=15_000)
        page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=10_000, state="attached")
    except Exception:
        log.warning("  go_back() didn't land on the jobs list — falling back to full reload")
        _load_jobs_list(page)
        return

    _pause(1.0, 1.5)
    # go_back() can restore mid-scroll state — finish scrolling to render the rest.
    prev = page.locator("[data-testid='UnifiedJobTldLink']").count()
    for _ in range(10):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        count = page.locator("[data-testid='UnifiedJobTldLink']").count()
        if count == prev:
            break
        prev = count


def _load_jobs_list(page: Page):
    """Navigate to JOBS_URL and scroll until all job links are loaded.
    Uses the same logic as get_jobs() so links are guaranteed to appear.
    """
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        page.wait_for_selector(
            "[data-testid='UnifiedJobTldLink']", timeout=20_000, state="attached"
        )
    except Exception:
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        try:
            page.wait_for_selector(
                "[data-testid='UnifiedJobTldLink']", timeout=15_000, state="attached"
            )
        except Exception:
            pass
    # Scroll to load all links (same as get_jobs)
    prev = 0
    for _ in range(10):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        count = page.locator("[data-testid='UnifiedJobTldLink']").count()
        if count == prev:
            break
        prev = count


def pull_job_descriptions(page: Page, jobs: list[dict]):
    """Pull JD for every job that isn't already cached in config/job_descriptions.json.

    Navigates via real link clicks from the jobs list (like a human browsing),
    NOT a raw fetch() to /jobs/view?employerJobId=... — Cloudflare returns
    HTTP 403 ("Security Check") on back-to-back fetch() calls fired with no
    delay between them (confirmed via diagnostic logging: every uncached job
    in a same-run burst got 403'd, while clicking through the same job in a
    real browser renders the JD with no challenge at all).

    Called immediately after get_jobs(), so the page is already on JOBS_URL
    with all job links loaded — no need to re-navigate at the start.
    """
    existing: dict = {}
    if JD_FILE.exists():
        try:
            existing = json.loads(JD_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _is_complete(eid: str) -> bool:
        e = existing.get(eid, {})
        return bool(e.get("job_description") and e.get("date_posted"))

    for job in jobs:
        if _is_complete(job["employer_job_id"]):
            log.info(f"  JD cached: {job['title']}")

    # Jobs cached before date_posted was tracked get one re-pull to backfill it.
    jobs_to_pull = [j for j in jobs if not _is_complete(j["employer_job_id"])]
    if not jobs_to_pull:
        return

    updated = False
    # The caller (get_jobs(), per this function's docstring) already navigated
    # to JOBS_URL and scrolled every link into view — reuse that for the first
    # iteration instead of immediately re-navigating to the same URL we're
    # already sitting on. Back-to-back navigations to the same page in quick
    # succession is the exact pattern that got the old fetch()-based JD pull
    # 403'd by Cloudflare; only reload once we've actually clicked away.
    on_list_page = True

    for job in jobs_to_pull:
        title = job["title"]
        eid   = job["employer_job_id"]
        log.info(f"  Pulling JD: {title}")
        try:
            if not on_list_page:
                _return_to_jobs_list(page)
            on_list_page = True  # confirmed on the list now — no navigation has happened yet this iteration

            target_link = None
            for link in page.locator("[data-testid='UnifiedJobTldLink']").all():
                href = link.get_attribute("href") or ""
                if "employerJobId=" not in href:
                    continue
                params = parse_qs(urlparse(href).query)
                if params.get("employerJobId", [""])[0] == eid:
                    target_link = link
                    break

            if target_link is None:
                log.warning(f"  JD pull: link not found for {title!r} (job no longer listed?)")
                try:
                    snap_dir = CANDIDATES_DIR.parent / "screenshots"
                    snap_dir.mkdir(parents=True, exist_ok=True)
                    snap_path = snap_dir / f"jd_link_not_found_{eid[-10:]}.png"
                    page.screenshot(path=str(snap_path), full_page=True)
                    log.info(f"  Debug screenshot: {snap_path}")
                except Exception:
                    pass
                continue  # still on_list_page=True — no navigation happened, correctly skip the reload next time

            target_link.click()
            on_list_page = False  # navigated away to the job detail page
            _pause(1.5, 2.5)
            try:
                page.wait_for_selector("text=Job description", timeout=10_000)
            except Exception:
                pass

            try:
                body = page.evaluate("document.body.innerText || document.body.textContent || ''")
            except Exception:
                body = ""

            date_posted = _extract_date_posted(body)

            jd = _extract_jd_text(page)
            if not jd or len(jd) < 100:
                jd = _extract_jd_from_body(body)

            if jd and len(jd) > 100:
                log.info(f"  JD pulled ({len(jd)} chars, posted={date_posted or 'unknown'})")
                existing[eid] = {
                    "title": title,
                    "employer_job_id": eid,
                    "job_description": jd,
                    "date_posted": date_posted,
                }
                updated = True
            else:
                log.warning(f"  JD empty for {title!r} (click-through extraction returned {len(jd or '')} chars)")

            _pause(1.0, 2.0)  # human-like gap between job-detail visits; next loop reloads the list

        except Exception as e:
            log.warning(f"  JD pull error [{title}]: {e}")
            on_list_page = False  # state is uncertain after an exception — force a reload next time

    if updated:
        JD_FILE.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info(f"  JDs saved → {JD_FILE}")


# ── quick "any new candidates?" check ────────────────────────────────────────

def has_new_candidates() -> bool:
    """
    Open the All tab for the first job, read the first candidate's Indeed ID,
    and check whether they are already in our CANDIDATES_DIR.
    Returns True if new candidates exist, False if we can skip the full scrape.
    """
    if not SESSION_STATE_FILE.exists():
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                storage_state=str(SESSION_STATE_FILE),
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()

            jobs = get_jobs(page)  # navigates to JOBS_URL internally

            if not is_session_valid(page):
                browser.close()
                return False
            if not jobs:
                browser.close()
                return False

            job = jobs[0]
            result = go_all_candidates(page, job)
            if result is None:
                browser.close()
                return True  # can't tell — assume yes

            base_url, _ = result
            # Navigate to All tab and read the first candidate row
            try:
                page.goto(f"{base_url}&status=all", wait_until="domcontentloaded", timeout=20_000)
                _pause(1, 2)
                first_row = page.locator("tr[data-indeed-id], [data-testid='candidate-row']").first
                indeed_id = (
                    first_row.get_attribute("data-indeed-id") or
                    first_row.get_attribute("data-id") or ""
                )
            except Exception:
                indeed_id = ""

            browser.close()

            if not indeed_id:
                return True  # can't tell — assume yes

            # Check if this candidate is already in our DB
            existing = list(CANDIDATES_DIR.glob(f"*{indeed_id}*.json"))
            return len(existing) == 0  # True = new, False = already seen

    except Exception as e:
        log.warning(f"has_new_candidates check failed: {e}")
        return True  # assume yes on error


# ── core scrape logic (callable from pipeline) ────────────────────────────────

def run(
    visible: bool = False,
    on_job_done=None,
    ask_roles: bool = False,
    on_candidate_saved=None,
    new_only: bool = False,
    role_timeout: float = 1800,
    n_candidates: int | None = None,
):
    """
    Scrape all active jobs.

    new_only      — only scrape the 'New' status tab (cron mode)
    role_timeout  — seconds to wait for yes/no before auto-proceeding (default 30 min)
    n_candidates  — if set, pick this many random candidates per role then stop
    on_job_done(title, eid, new_paths)  — called after every role completes
    """
    if not SESSION_STATE_FILE.exists():
        log.error(f"No session at {SESSION_STATE_FILE}. Run: python scraper/indeed_login.py")
        return

    last_run = load_last_run()
    # "candidate_id:job_id" keys — dedup within a job, not across jobs.
    # Seeded from disk so interrupted runs don't re-scrape already-saved candidates.
    seen_keys: set[str] = set(last_run.get("applicants_seen", []))
    for _f in CANDIDATES_DIR.glob("*.json"):
        try:
            _d = json.loads(_f.read_text(encoding="utf-8"))
            _cid, _jid = _d.get("id"), _d.get("job_id")
            if _cid and _jid:
                seen_keys.add(f"{_cid}:{_jid}")
        except Exception:
            pass
    log.info(f"Resuming with {len(seen_keys)} already-seen candidate/job pair(s)")

    candidates_index = load_candidates_index()
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)

    total_new = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not visible,
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
        if _STEALTH:
            _Stealth().apply_stealth_sync(page)

        jobs = get_jobs(page)  # navigates to JOBS_URL internally

        if not is_session_valid(page):
            log.error(f"Session invalid. URL: {page.url}. Re-run: python scraper/indeed_login.py")
            browser.close()
            return
        if not jobs:
            log.error("No active jobs found.")
            browser.close()
            return

        log.info("\nPulling job descriptions...")
        try:
            pull_job_descriptions(page, jobs)
        except Exception as e:
            log.warning(f"JD pull step failed (non-fatal): {e}")

        for job_idx, job in enumerate(jobs, 1):
            title = job["title"]
            eid   = job["employer_job_id"]
            log.info(f"\n{'='*60}")
            log.info(f"[{job_idx}/{len(jobs)}] {title}")
            log.info(f"{'='*60}")

            # Name -> (path, data) for everyone already saved under this job.
            # Lets us tell "unchanged" / "moved tabs" / "genuinely new" apart
            # from the row alone, with zero browser navigation for the first
            # two cases — only a truly new name needs its profile opened.
            job_slug = hashlib.md5(eid.encode()).hexdigest()[:8]
            existing_by_name: dict[str, dict] = {}
            for f in CANDIDATES_DIR.glob(f"*-{job_slug}.json"):
                d = load_json(f, {})
                name = d.get("full_name")
                if name:
                    existing_by_name[name] = {"path": f, "data": d}

            if ask_roles:
                mins = int(role_timeout / 60)
                ans = _ask_timeout(
                    f"  Scrape {title!r}? [Y/n] (auto-yes in {mins}m): ",
                    timeout=role_timeout,
                    default="y",
                ).strip().lower()
                if ans in ("n", "no"):
                    log.info(f"  Skipped by user.")
                    continue

            log.info(f"  Scraping...")

            result = go_all_candidates(page, job)
            if result is None:
                log.warning(f"[{job_idx}/{len(jobs)}] {title} — skipped (could not navigate)")
                continue

            base_url, job_counts = result

            # Compare against the last-known counts for this job *before*
            # overwriting them, so incremental runs can tell which tabs moved
            # since last time (a candidate can change status — e.g. Rejected
            # -> Reviewing — without "new" ever changing, and new_only mode
            # used to only ever look at the New tab, missing that entirely).
            all_counts = load_status_counts()
            old_counts = all_counts.get(eid, {}).get("counts", {})

            if new_only and job_counts == old_counts:
                log.info(f"  No changes since last run — skipping")
                continue

            all_counts[eid] = {
                "title":      title,
                "updated_at": now_iso(),
                "counts":     job_counts,
            }
            save_status_counts(all_counts)

            job_new       = 0
            job_skip      = 0
            job_updated   = 0
            job_withdrawn = 0
            job_paths: list[Path] = []

            # Which tabs to visit: full scrape visits everything; incremental
            # runs only revisit tabs whose count actually changed since the
            # last run (covers candidates moving between statuses, not just
            # new applicants).
            if new_only:
                tabs_to_scrape = [
                    (tab_name, tab_status) for tab_name, tab_status in SCRAPE_TABS
                    if job_counts.get(tab_status, 0) != old_counts.get(tab_status, 0)
                ]
                log.info(f"  Changed tabs: {[t for t, _ in tabs_to_scrape] or 'none'}")
            else:
                tabs_to_scrape = SCRAPE_TABS

            for tab_name, tab_status in tabs_to_scrape:
                tab_count = job_counts.get(tab_status, 0)
                if tab_count == 0:
                    log.info(f"  [{tab_name}]: 0 candidates — skipping tab")
                    continue

                log.info(f"  [{tab_name}]: {tab_count} candidate(s)")

                if not _click_tab(page, tab_name):
                    log.warning(f"  Could not click '{tab_name}' tab — skipping")
                    try:
                        snap_dir = CANDIDATES_DIR.parent / "screenshots"
                        snap_dir.mkdir(parents=True, exist_ok=True)
                        page.screenshot(
                            path=str(snap_dir / f"debug_tab_{tab_name.lower()}_{eid[:12]}.png"),
                            full_page=True,
                        )
                    except Exception:
                        pass
                    continue

                try:
                    page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=8_000)
                except Exception:
                    pass

                tab_url  = page.url
                page_num = 1

                while True:
                    if not _page_alive(page):
                        page = context.new_page()
                        page.goto(tab_url, wait_until="domcontentloaded", timeout=30_000)
                        _pause(1.5, 2.5)
                        _dismiss_popup(page)
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
                        # status from row text; fall back to the tab we're on
                        status = next((l for l in lines if l in VALID_STATUSES), tab_status)
                        page_candidates.append((name, status))

                    log.info(f"  [{tab_name}] Page {page_num}: {len(page_candidates)} candidate(s)")

                    # --n mode: random sample per tab per page
                    if n_candidates is not None and len(page_candidates) > n_candidates:
                        page_candidates = random.sample(page_candidates, n_candidates)
                        log.info(f"  --n {n_candidates}: picked {len(page_candidates)} from [{tab_name}]")

                    for cand_name, cand_status in page_candidates:
                        if cand_status == "Withdrawn":
                            # No profile to open — Indeed renders these as inert
                            # rows with no action icons or click target. Record
                            # a minimal entry instead of treating it as a
                            # clickable candidate (which silently breaks back-
                            # navigation for whoever comes after it in the list).
                            if cand_name in existing_by_name:
                                log.info(f"  [withdrawn] Already recorded: {cand_name}")
                                continue

                            indeed_id = f"indeed-{hashlib.md5(cand_name.encode()).hexdigest()[:12]}"
                            seen_key  = f"{indeed_id}:{eid}"
                            out_path  = CANDIDATES_DIR / f"{indeed_id}-{job_slug}.json"
                            candidate_data = {
                                "id":         indeed_id,
                                "source":     "indeed",
                                "scraped_at": now_iso(),
                                "full_name":  cand_name,
                                "status":     "withdrawn",
                                "job_id":     eid,
                                "job_title":  title,
                            }
                            save_json(out_path, candidate_data)
                            _debug_verify_saved(out_path, f"withdrawn:{cand_name}")
                            log.info(f"  [withdrawn] Recorded (no profile to scrape): {cand_name}")

                            if on_candidate_saved:
                                on_candidate_saved(out_path)

                            candidates_index.append({
                                "id":         indeed_id,
                                "full_name":  cand_name,
                                "source":     "indeed",
                                "scraped_at": candidate_data["scraped_at"],
                                "status":     "withdrawn",
                                "job_title":  title,
                                "fit_score":  None,
                            })
                            append_audit_log({
                                "event":     "candidate_withdrawn",
                                "id":        indeed_id,
                                "full_name": cand_name,
                                "status":    "withdrawn",
                                "job_id":    eid,
                                "job_title": title,
                            })

                            seen_keys.add(seen_key)
                            existing_by_name[cand_name] = {"path": out_path, "data": candidate_data}
                            job_withdrawn += 1
                            job_paths.append(out_path)
                            continue

                        existing_entry = existing_by_name.get(cand_name)
                        if existing_entry is not None:
                            existing    = existing_entry["data"]
                            out_path    = existing_entry["path"]
                            old_status  = (existing.get("status") or "").lower()
                            new_status  = cand_status.lower()
                            indeed_id   = existing.get("id", "")
                            seen_key    = f"{indeed_id}:{eid}"

                            if old_status == new_status:
                                job_skip += 1
                                continue

                            existing["status"]     = new_status
                            existing["scraped_at"] = now_iso()
                            save_json(out_path, existing)
                            _debug_verify_saved(out_path, f"status-update:{cand_name}")
                            log.info(f"  [status] {cand_name}: {old_status} -> {new_status}")

                            for entry in candidates_index:
                                if entry.get("id") == indeed_id:
                                    entry["status"] = new_status
                                    break
                            append_audit_log({
                                "event":      "candidate_status_updated",
                                "id":         indeed_id,
                                "full_name":  cand_name,
                                "old_status": old_status,
                                "new_status": new_status,
                                "job_id":     eid,
                                "job_title":  title,
                            })

                            seen_keys.add(seen_key)
                            job_updated += 1
                            continue

                        try:
                            row = _find_row_by_name(page, cand_name)

                            if row is None:
                                # List/tab state may have been lost after the previous
                                # "back" navigation — reload and re-click the tab, then
                                # retry once before giving up on this candidate.
                                page.goto(tab_url, wait_until="domcontentloaded", timeout=30_000)
                                _pause(1.5, 2.5)
                                _dismiss_popup(page)
                                _click_tab(page, tab_name)
                                page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=15_000)
                                row = _find_row_by_name(page, cand_name)

                            if row is None:
                                log.warning(f"  [no-match] Row for {cand_name!r} not found in DOM — skipping")
                                continue

                            row.locator("td").first.click()
                            _pause(2.0, 3.5)

                            candidate_data = scrape_profile(page, cand_name, cand_status, eid, title)
                            indeed_id = candidate_data["id"]
                            seen_key  = f"{indeed_id}:{eid}"
                            out_path  = CANDIDATES_DIR / f"{indeed_id}-{job_slug}.json"

                            if seen_key in seen_keys:
                                existing = load_json(out_path, {})
                                old_status = (existing.get("status") or "").lower()
                                new_status = (candidate_data.get("status") or "").lower()

                                if not existing:
                                    # seen_keys says we've processed this id:job pair
                                    # before, but there's no file on disk for it (stale
                                    # last_run.json entry, or a previous save never
                                    # actually persisted) — recover instead of treating
                                    # it as "already saved" forever and going invisible.
                                    save_json(out_path, candidate_data)
                                    _debug_verify_saved(out_path, f"recovered-missing:{cand_name}")
                                    log.warning(f"  [recovered] {cand_name}: seen_key was stale (no file on disk) — saved fresh")
                                    if on_candidate_saved:
                                        on_candidate_saved(out_path)
                                    candidates_index.append({
                                        "id":         indeed_id,
                                        "full_name":  candidate_data["full_name"],
                                        "source":     "indeed",
                                        "scraped_at": candidate_data["scraped_at"],
                                        "status":     candidate_data["status"],
                                        "job_title":  title,
                                        "fit_score":  None,
                                    })
                                    append_audit_log({
                                        "event":     "candidate_scraped",
                                        "id":        indeed_id,
                                        "full_name": candidate_data["full_name"],
                                        "status":    candidate_data["status"],
                                        "job_id":    eid,
                                        "job_title": title,
                                    })
                                    job_new += 1
                                elif old_status != new_status:
                                    existing["status"]     = new_status
                                    existing["scraped_at"] = candidate_data["scraped_at"]
                                    save_json(out_path, existing)
                                    _debug_verify_saved(out_path, f"status-update-seen:{cand_name}")
                                    log.info(f"  [status] {cand_name}: {old_status} -> {new_status}")

                                    for entry in candidates_index:
                                        if entry.get("id") == indeed_id:
                                            entry["status"] = new_status
                                            break
                                    append_audit_log({
                                        "event":     "candidate_status_updated",
                                        "id":        indeed_id,
                                        "full_name": cand_name,
                                        "old_status": old_status,
                                        "new_status": new_status,
                                        "job_id":    eid,
                                        "job_title": title,
                                    })
                                    job_updated += 1
                                else:
                                    log.info(f"  [skip] Already saved: {cand_name}")
                                    job_skip += 1

                                back = page.locator("[data-testid='BackToListButton']").first
                                if back.count() > 0:
                                    back.click()
                                else:
                                    page.go_back()
                                _pause(1.5, 2.5)
                                continue

                            save_json(out_path, candidate_data)
                            _debug_verify_saved(out_path, f"new:{cand_name}")
                            log.info(f"  + Saved: {cand_name} [{tab_name} -> {cand_status}]")

                            if on_candidate_saved:
                                on_candidate_saved(out_path)

                            candidates_index.append({
                                "id":         indeed_id,
                                "full_name":  candidate_data["full_name"],
                                "source":     "indeed",
                                "scraped_at": candidate_data["scraped_at"],
                                "status":     candidate_data["status"],
                                "job_title":  title,
                                "fit_score":  None,
                            })
                            append_audit_log({
                                "event":     "candidate_scraped",
                                "id":        indeed_id,
                                "full_name": candidate_data["full_name"],
                                "status":    candidate_data["status"],
                                "job_id":    eid,
                                "job_title": title,
                            })

                            seen_keys.add(seen_key)
                            existing_by_name[cand_name] = {"path": out_path, "data": candidate_data}
                            job_new   += 1
                            total_new += 1
                            job_paths.append(out_path)

                            if not _page_alive(page):
                                raise RuntimeError("page died")

                            back = page.locator("[data-testid='BackToListButton']").first
                            if back.count() > 0:
                                back.click()
                            else:
                                page.go_back()
                            _pause(1.5, 2.5)
                            _dismiss_popup(page)
                            page.wait_for_selector(
                                "[data-testid='candidate-list-table-container']", timeout=20_000
                            )

                        except Exception as e:
                            log.warning(f"  Error [{cand_name}]: {e}")
                            # Recover back to the list, then re-click the correct tab
                            # (Indeed's tab filter is client-side and lost on page reload)
                            if not _page_alive(page):
                                page = context.new_page()
                                page.goto(tab_url, wait_until="domcontentloaded", timeout=30_000)
                                _pause(1.5, 2.5)
                                _dismiss_popup(page)
                                _click_tab(page, tab_name)
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
                                        "[data-testid='candidate-list-table-container']", timeout=20_000
                                    )
                                except Exception:
                                    page = context.new_page()
                                    page.goto(tab_url, wait_until="domcontentloaded", timeout=30_000)
                                    _pause(1.5, 2.5)
                                    _dismiss_popup(page)
                                    _click_tab(page, tab_name)

                    next_btn = page.locator("button:has-text('Next'), a:has-text('Next')").last
                    if next_btn.count() > 0 and next_btn.is_enabled():
                        next_btn.click()
                        page_num += 1
                        _pause(2.0, 3.5)
                    else:
                        break

                _pause(1.0, 2.0)   # brief pause between tabs

            log.info(
                f"[{job_idx}/{len(jobs)}] {title} — DONE "
                f"({job_new} new, {job_skip} already saved, {job_updated} status updates, "
                f"{job_withdrawn} withdrawn)"
            )

            if on_job_done:
                on_job_done(title, eid, job_paths)

            if job_idx < len(jobs):
                log.info("  Cooling down...")
                _pause(5.0, 9.0)

        browser.close()

    last_run["last_run_at"]     = now_iso()
    last_run["applicants_seen"] = list(seen_keys)
    save_last_run(last_run)
    save_candidates_index(candidates_index)
    log.info(f"\nScraper done — {total_new} new candidate(s) total.")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape all Indeed candidates — full profiles")
    parser.add_argument("--visible",      action="store_true", help="Show browser window")
    parser.add_argument("--ask-roles",    action="store_true", help="Prompt before scraping each role")
    parser.add_argument("--new-only",     action="store_true", help="Only scrape 'New' tab (cron mode)")
    parser.add_argument("--role-timeout", type=float, default=1800,
                        help="Seconds to wait for yes/no before auto-proceeding (default 1800 = 30 min)")
    parser.add_argument("-n", "--n-candidates", type=int, default=None,
                        help="Random-pick N candidates per role instead of scraping all")
    args = parser.parse_args()

    if not SESSION_STATE_FILE.exists():
        log.error(f"No session at {SESSION_STATE_FILE}. Run: python scraper/indeed_login.py")
        sys.exit(1)

    try:
        load_credentials()
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    run(
        visible=args.visible,
        ask_roles=args.ask_roles,
        new_only=args.new_only,
        role_timeout=args.role_timeout,
        n_candidates=args.n_candidates,
    )


if __name__ == "__main__":
    main()
