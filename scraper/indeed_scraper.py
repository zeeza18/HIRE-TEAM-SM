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
    append_audit_log, save_json, now_iso,
    load_status_counts, save_status_counts,
)

log = get_logger("indeed_scraper")

RESUMES_DIR    = CANDIDATES_DIR.parent / "resumes"
JD_FILE        = CONFIG_DIR / "job_descriptions.json"
VALID_STATUSES = {"New", "Reviewing", "Contacting", "Interviewing",
                  "Rejected", "Hired", "Invited", "Pending"}
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
    """Return all open+paused jobs (cancelled excluded by JOBS_URL filter)."""
    log.info("Scanning jobs table...")
    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)

    jobs: list[dict] = []
    seen_ids: set[str] = set()

    try:
        # state="attached" — element exists in DOM even if not in viewport
        try:
            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=40_000, state="attached")
        except Exception:
            # One retry: wait for network idle then try again
            page.wait_for_load_state("networkidle", timeout=15_000)
            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=20_000, state="attached")
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
                log.info(f"  • {title}")
    except Exception as e:
        log.warning(f"Job scan error: {e}")

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


def _click_tab(page: Page, tab_name: str) -> bool:
    """Click a status filter tab by name. Returns True if found and clicked."""
    # Indeed uses <label> radio elements for tabs.
    # Count the parent locator (not .first) to avoid Playwright's .first.count()==1 quirk.
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
    return False


def go_all_candidates(page: Page, job: dict) -> tuple[str, dict] | None:
    """Navigate to the job's candidates page.
    Returns (base_url, status_counts) or None on failure."""
    title = job["title"]
    eid   = job["employer_job_id"]

    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
    _pause(1.5, 2.5)
    page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=40_000, state="attached")

    # ── Click the job link to reach the candidates page ─────────────────────
    job_link = None
    for lnk in page.locator("[data-testid='UnifiedJobTldLink']").all():
        href = lnk.get_attribute("href") or ""
        if parse_qs(urlparse(href).query).get("employerJobId", [""])[0] == eid:
            job_link = lnk
            break

    if job_link is None:
        log.warning(f"Link not found for {title!r}")
        return None

    job_link.click()
    _pause(2.5, 4.5)
    _dismiss_popup(page)

    if not is_session_valid(page):
        log.error("Session expired mid-run.")
        return None

    # ── Ensure we're on the candidates list ──────────────────────────────────
    # After clicking the job link we may land on the posting edit page.
    # Navigate to candidates via the sidebar link if needed.
    if "[data-testid='candidate-list-table-container']" not in (page.content()[:200] or ""):
        try:
            page.wait_for_selector("[data-testid='candidate-list-table-container']", timeout=8_000)
        except Exception:
            # Not there — try sidebar "Candidates" / "Manage candidates" nav link
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
    """Extract JD from job detail page using selectors confirmed in debug_jd.py."""
    best = ""
    for sel in [".css-1jpbxfu", ".css-19qk1my"]:
        try:
            for el in page.locator(sel).all():
                txt = el.inner_text().strip()
                if len(txt) > len(best):
                    best = txt
        except Exception:
            pass
    if len(best) > 100:
        return best

    # Fallback: walk up from "Job description:" label
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
    return ""


def pull_job_descriptions(page: Page, jobs: list[dict]):
    """Pull JD for every job that isn't already cached in config/job_descriptions.json."""
    existing: dict = {}
    if JD_FILE.exists():
        try:
            existing = json.loads(JD_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    updated = False
    for job in jobs:
        title = job["title"]
        eid   = job["employer_job_id"]

        if existing.get(eid, {}).get("job_description"):
            log.info(f"  JD cached: {title}")
            continue

        log.info(f"  Pulling JD: {title}")
        try:
            nav_btn = page.locator("[data-testid='menu-link-AllJobs']").first
            if nav_btn.count() > 0:
                nav_btn.click()
            else:
                page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
            _pause(1.5, 2.5)
            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=40_000, state="attached")

            clicked = False
            for lnk in page.locator("[data-testid='UnifiedJobTldLink']").all():
                href = lnk.get_attribute("href") or ""
                if parse_qs(urlparse(href).query).get("employerJobId", [""])[0] == eid:
                    lnk.click()
                    clicked = True
                    break

            if not clicked:
                log.warning(f"  Could not navigate to {title!r} for JD pull")
                continue

            _pause(2.5, 3.5)
            jd = _extract_jd_text(page)
            if jd:
                log.info(f"  ✓ JD pulled ({len(jd)} chars)")
                existing[eid] = {
                    "title": title,
                    "employer_job_id": eid,
                    "job_description": jd,
                }
                updated = True
            else:
                log.warning(f"  ✗ JD empty for {title!r}")
        except Exception as e:
            log.warning(f"  JD pull error [{title}]: {e}")

    if updated:
        JD_FILE.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info(f"  JDs saved → {JD_FILE}")


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

        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
        _pause(1.5, 2.5)

        if not is_session_valid(page):
            log.error(f"Session invalid. URL: {page.url}. Re-run: python scraper/indeed_login.py")
            browser.close()
            return

        jobs = get_jobs(page)
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

            # In new-only cron mode, skip silently if Indeed shows 0 new
            if new_only and job_counts.get("new", 1) == 0:
                log.info(f"  No new candidates — skipping")
                continue

            # Persist latest Indeed tab counts for this job
            all_counts = load_status_counts()
            all_counts[eid] = {
                "title":      title,
                "updated_at": now_iso(),
                "counts":     job_counts,
            }
            save_status_counts(all_counts)

            job_new   = 0
            job_skip  = 0
            job_paths: list[Path] = []

            # Which tabs to visit: new-only cron vs full scrape
            tabs_to_scrape = [("New", "new")] if new_only else SCRAPE_TABS

            for tab_name, tab_status in tabs_to_scrape:
                tab_count = job_counts.get(tab_status, 0)
                if tab_count == 0:
                    log.info(f"  [{tab_name}]: 0 candidates — skipping tab")
                    continue

                log.info(f"  [{tab_name}]: {tab_count} candidate(s)")

                if not _click_tab(page, tab_name):
                    log.warning(f"  Could not click '{tab_name}' tab — skipping")
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
                        try:
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
                                continue

                            candidate_data = scrape_profile(page, cand_name, cand_status, eid, title)
                            indeed_id = candidate_data["id"]
                            seen_key  = f"{indeed_id}:{eid}"

                            if seen_key in seen_keys:
                                log.info(f"  ✓ Already saved: {cand_name} — skipping")
                                job_skip += 1
                                back = page.locator("[data-testid='BackToListButton']").first
                                if back.count() > 0:
                                    back.click()
                                else:
                                    page.go_back()
                                _pause(1.5, 2.5)
                                continue

                            job_slug = hashlib.md5(eid.encode()).hexdigest()[:8]
                            out_path = CANDIDATES_DIR / f"{indeed_id}-{job_slug}.json"
                            save_json(out_path, candidate_data)
                            log.info(f"  + Saved: {cand_name} [{tab_name}→{cand_status}]")

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
                f"({job_new} new, {job_skip} already saved)"
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
