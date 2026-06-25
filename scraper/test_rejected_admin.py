"""
Isolated test: verify the 'Rejected' tab scrape for the Administrator job
finds every candidate Indeed reports, exercising the same virtualized-list
fix (_find_row_by_name) used by the real scraper. Does not save candidate
data — just walks the list, clicking into and back from each row, and
confirms the matched count equals the tab's reported count.

Usage:
    python scraper/test_rejected_admin.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from scraper.utils import SESSION_STATE_FILE, STATUS_COUNTS_FILE, get_logger, load_json
from scraper.indeed_scraper import (
    _click_tab, _find_row_by_name, _dismiss_popup, _read_status_counts,
    _LIST_CONTAINER_SEL, is_session_valid, _pause, HEADER_NAMES,
)

log = get_logger("test_rejected_admin")

ROLE_TITLE = "Administrator"
TAB_NAME   = "Rejected"


def snap(page, name: str):
    out = Path("data/screenshots") / f"{name}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), full_page=True)
    log.info(f"Screenshot: {out}")


def _find_admin_eid() -> str:
    counts = load_json(STATUS_COUNTS_FILE, {})
    for eid, info in counts.items():
        if info.get("title") == ROLE_TITLE:
            return eid
    raise RuntimeError(f"No cached job_status_counts entry for {ROLE_TITLE!r}")


def run():
    if not SESSION_STATE_FILE.exists():
        log.error("No session. Run: python scraper/indeed_login.py")
        return

    eid = _find_admin_eid()
    log.info(f"{ROLE_TITLE} employer_job_id: {eid[:20]}...")

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

        candidates_url = f"https://employers.indeed.com/candidates?employerJobId={eid}"
        page.goto(candidates_url, wait_until="domcontentloaded", timeout=30_000)
        _pause(2.5, 4.0)
        _dismiss_popup(page)

        if not is_session_valid(page):
            log.error("Session invalid/expired.")
            snap(page, "00_session_invalid")
            browser.close()
            return

        page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=20_000)
        snap(page, "01_candidates_loaded")

        counts = _read_status_counts(page)
        expected = counts.get("rejected")
        log.info(f"Tab header counts: {counts}")
        log.info(f"Expected Rejected count: {expected}")

        if not _click_tab(page, TAB_NAME):
            log.error(f"Could not click '{TAB_NAME}' tab")
            snap(page, "02_tab_click_failed")
            browser.close()
            return

        page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=15_000)
        snap(page, "02_rejected_tab_loaded")

        tab_url   = page.url
        found     = []
        no_match  = []
        page_num  = 1

        while True:
            rows = page.locator(f"{_LIST_CONTAINER_SEL} [role='row']").all()
            page_names = []
            for row in rows:
                try:
                    lines = [l.strip() for l in row.inner_text().strip().split("\n") if l.strip()]
                except Exception:
                    continue
                if not lines or lines[0] in HEADER_NAMES:
                    continue
                page_names.append(lines[0])

            log.info(f"[Rejected] Page {page_num}: {len(page_names)} candidate(s) — {page_names}")

            for name in page_names:
                row = _find_row_by_name(page, name)
                if row is None:
                    log.warning(f"  [no-match] {name!r} could not be located")
                    no_match.append(name)
                    continue

                row.locator("td").first.click()
                _pause(2.0, 3.0)

                if len(found) < 3:
                    snap(page, f"03_profile_{len(found) + 1:02d}_{name.replace(' ', '_')[:20]}")

                found.append(name)

                back = page.locator("[data-testid='BackToListButton']").first
                if back.count() > 0:
                    back.click()
                else:
                    page.go_back()
                _pause(1.5, 2.5)
                _dismiss_popup(page)
                try:
                    page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=20_000)
                except Exception:
                    snap(page, f"99_back_timeout_after_{name.replace(' ', '_')[:20]}")
                    log.warning(f"  List didn't reload after viewing {name!r} — retrying via tab url")
                    page.goto(tab_url, wait_until="domcontentloaded", timeout=30_000)
                    _pause(1.5, 2.5)
                    _dismiss_popup(page)
                    _click_tab(page, TAB_NAME)
                    page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=20_000)

            next_btn = page.locator("button:has-text('Next'), a:has-text('Next')").last
            if next_btn.count() > 0 and next_btn.is_enabled():
                next_btn.click()
                page_num += 1
                _pause(2.0, 3.0)
            else:
                break

        snap(page, "04_final_state")
        browser.close()

    log.info("=" * 60)
    log.info(f"RESULT: matched {len(found)} / expected {expected}  ({len(no_match)} no-match)")
    if no_match:
        log.warning(f"Missed: {no_match}")
    if expected is not None and len(found) == expected:
        log.info("PASS — all Rejected candidates for Administrator were found.")
    else:
        log.error("FAIL — matched count does not equal Indeed's reported Rejected count.")
    log.info("=" * 60)


if __name__ == "__main__":
    run()
