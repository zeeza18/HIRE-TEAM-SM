"""
Debug script — pulls Job Description text from every active job on Indeed.

Saves results to:
    config/job_descriptions.json   ← keyed by employer_job_id
    data/debug_jd_<title>.png      ← screenshot of JD section per job

Run:
    python scraper/debug_jd.py
"""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright
from scraper.utils import SESSION_STATE_FILE, JOBS_URL, CONFIG_DIR, DATA_DIR, get_logger

log = get_logger("debug_jd")

SCREENSHOTS_DIR = DATA_DIR / "screenshots"
JD_FILE = CONFIG_DIR / "job_descriptions.json"


def _safe_name(title: str) -> str:
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in title).strip()[:40]


def _extract_jd(page) -> str:
    """Try every known selector for the JD container, return best result."""

    # Strategy 1: exact CSS classes from codegen recording — try longest text wins
    best_css = ""
    for sel in [".css-1jpbxfu", ".css-19qk1my"]:
        try:
            els = page.locator(sel).all()
            for el in els:
                txt = el.inner_text().strip()
                if len(txt) > len(best_css):
                    best_css = txt
        except Exception:
            pass
    if len(best_css) > 100:
        log.info(f"  JD found via codegen CSS classes ({len(best_css)} chars)")
        return best_css

    # Strategy 2: anchor on "Job description:" label then grab sibling/parent text
    try:
        label = page.get_by_text("Job description:", exact=False).first
        if label.count() > 0:
            # Walk up to a container that has meaningful content
            for depth in range(1, 6):
                js = f"""
                (el) => {{
                    let node = el;
                    for (let i = 0; i < {depth}; i++) {{
                        if (node.parentElement) node = node.parentElement;
                    }}
                    return node.innerText;
                }}
                """
                txt = page.evaluate(js, label.element_handle())
                if txt and len(txt) > 200:
                    log.info(f"  JD found via label parent depth={depth} ({len(txt)} chars)")
                    return txt.strip()
    except Exception as e:
        log.debug(f"  label strategy failed: {e}")

    # Strategy 3: data-testid patterns common in Indeed employer pages
    for testid in [
        "job-description",
        "jobDescription",
        "job-details-description",
        "job-info-description",
    ]:
        try:
            el = page.locator(f"[data-testid='{testid}']").first
            if el.count() > 0:
                txt = el.inner_text().strip()
                if len(txt) > 100:
                    log.info(f"  JD found via data-testid={testid} ({len(txt)} chars)")
                    return txt
        except Exception:
            pass

    # Strategy 4: any element containing "Job description:" with long content
    try:
        containers = page.locator("div, section, article").all()
        best = ""
        for c in containers:
            try:
                txt = c.inner_text()
                if "Job description" in txt and len(txt) > len(best):
                    best = txt
            except Exception:
                continue
        if len(best) > 200:
            log.info(f"  JD found via text-scan ({len(best)} chars)")
            return best.strip()
    except Exception as e:
        log.debug(f"  text-scan failed: {e}")

    return ""


def main():
    if not SESSION_STATE_FILE.exists():
        log.error(f"No session — run: python scraper/indeed_login.py")
        sys.exit(1)

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    results: dict = {}

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

        # ── collect jobs ──────────────────────────────────────────────────────
        log.info("Loading jobs page...")
        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(2)

        try:
            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)
        except Exception:
            log.error("Could not find job links — session may have expired")
            page.screenshot(path=str(SCREENSHOTS_DIR / "debug_jd_error.png"))
            browser.close()
            sys.exit(1)

        # Scroll to load all jobs
        for _ in range(5):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)

        job_links = page.locator("[data-testid='UnifiedJobTldLink']").all()
        jobs = []
        for link in job_links:
            href  = link.get_attribute("href") or ""
            title = link.inner_text().strip()
            if "employerJobId=" not in href:
                continue
            params = parse_qs(urlparse(href).query)
            eid = params.get("employerJobId", [""])[0]
            if eid:
                jobs.append({"title": title, "eid": eid, "href": href})

        log.info(f"Found {len(jobs)} job(s)")

        # ── per-job JD extraction ─────────────────────────────────────────────
        for idx, job in enumerate(jobs, 1):
            title = job["title"]
            eid   = job["eid"]
            log.info(f"\n[{idx}/{len(jobs)}] {title}")

            # ── Exact navigation from codegen recording ────────────────────
            # Step 1: hit the All Jobs nav link
            try:
                all_jobs_btn = page.locator("[data-testid='menu-link-AllJobs']").first
                if all_jobs_btn.count() > 0:
                    all_jobs_btn.click()
                    time.sleep(2)
                    log.info("  Navigated via menu-link-AllJobs")
                else:
                    page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
                    time.sleep(2)
            except Exception:
                page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)

            page.wait_for_selector("[data-testid='UnifiedJobTldLink']", timeout=15_000)

            # Step 2: click job by its link name (codegen: get_by_role("link", name="Administrator"))
            clicked = False
            try:
                job_link = page.get_by_role("link", name=title, exact=True).first
                if job_link.count() > 0:
                    job_link.click()
                    time.sleep(3)
                    clicked = True
                    log.info(f"  Clicked job link by role name: {title!r}")
            except Exception:
                pass

            # Fallback: UnifiedJobTldLink with employerJobId
            if not clicked:
                link = page.locator(
                    f"[data-testid='UnifiedJobTldLink'][href*='{eid[:30]}']"
                ).first
                if link.count() == 0:
                    log.warning(f"  Job link not found, skipping")
                    continue
                link.click()
                time.sleep(3)

            log.info(f"  URL: {page.url}")

            # Screenshot before extraction
            snap_path = SCREENSHOTS_DIR / f"debug_jd_{_safe_name(title)}.png"
            page.screenshot(path=str(snap_path), full_page=True)
            log.info(f"  Screenshot → {snap_path.name}")

            # Dump ALL visible CSS class names that contain meaningful text (debug aid)
            log.info("  Probing CSS classes on page...")
            try:
                classes_info = page.evaluate("""() => {
                    const results = [];
                    document.querySelectorAll('[class]').forEach(el => {
                        const txt = (el.innerText || '').trim();
                        if (txt.length > 150 && txt.length < 5000) {
                            results.push({
                                tag: el.tagName,
                                cls: el.className.substring(0, 80),
                                len: txt.length,
                                preview: txt.substring(0, 80)
                            });
                        }
                    });
                    return results.slice(0, 20);
                }""")
                for info in classes_info:
                    log.info(f"    [{info['tag']}] .{info['cls']} ({info['len']} chars) → {info['preview']!r}")
            except Exception as e:
                log.debug(f"  class probe failed: {e}")

            jd_text = _extract_jd(page)

            if jd_text:
                log.info(f"  ✓ JD extracted ({len(jd_text)} chars)")
                log.info(f"  Preview: {jd_text[:200]!r}")
                results[eid] = {
                    "title":       title,
                    "employer_job_id": eid,
                    "job_description": jd_text,
                    "url":         page.url,
                }
            else:
                log.warning(f"  ✗ Could not extract JD — check screenshot: {snap_path.name}")
                log.warning("    Add the correct CSS selector to _extract_jd() based on the screenshot")
                results[eid] = {
                    "title":       title,
                    "employer_job_id": eid,
                    "job_description": "",
                    "url":         page.url,
                }

        input("\nDone — press Enter to close browser...")
        browser.close()

    # ── save results ──────────────────────────────────────────────────────────
    JD_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(JD_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    log.info(f"\nSaved → {JD_FILE}")

    # Print summary
    print("\n" + "="*60)
    print("JD EXTRACTION SUMMARY")
    print("="*60)
    for eid, data in results.items():
        status = f"{len(data['job_description'])} chars" if data["job_description"] else "EMPTY — needs fix"
        print(f"  {data['title']:<35} {status}")
    print(f"\nFull output → {JD_FILE}")
    print("Screenshots → data/screenshots/")


if __name__ == "__main__":
    main()
