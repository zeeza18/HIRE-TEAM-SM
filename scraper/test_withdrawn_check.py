"""
Verify the hypothesis: rows like 'Shelly Thompson', 'Matthew Taylor',
'LESLIE MANCHA' in Administrator's Rejected tab are Withdrawn applications
with no clickable profile (not a virtualization/timing fluke).

For each name: locate the row, print its raw text, attempt the click, and
screenshot before/after so we can see directly whether anything navigated.

Usage:
    python scraper/test_withdrawn_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.sync_api import sync_playwright

from scraper.utils import SESSION_STATE_FILE, STATUS_COUNTS_FILE, get_logger, load_json
from scraper.indeed_scraper import (
    _click_tab, _find_row_by_name, _dismiss_popup,
    _LIST_CONTAINER_SEL, is_session_valid, _pause,
)

log = get_logger("test_withdrawn_check")

ROLE_TITLE = "Administrator"
TAB_NAME   = "Rejected"
NAMES_TO_CHECK = ["Shelly Thompson", "Matthew Taylor", "LESLIE MANCHA"]


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
    eid = _find_admin_eid()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(SESSION_STATE_FILE))
        page = context.new_page()

        page.goto(
            f"https://employers.indeed.com/candidates?employerJobId={eid}",
            wait_until="domcontentloaded", timeout=30_000,
        )
        _pause(2.5, 4.0)
        _dismiss_popup(page)

        if not is_session_valid(page):
            log.error("Session invalid.")
            browser.close()
            return

        page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=20_000)
        _click_tab(page, TAB_NAME)
        page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=20_000)
        tab_url = page.url

        for name in NAMES_TO_CHECK:
            log.info("=" * 60)
            log.info(f"Checking: {name!r}")

            row = _find_row_by_name(page, name)
            if row is None:
                log.warning(f"  Row not found at all for {name!r}")
                continue

            raw_text = row.inner_text().strip()
            log.info(f"  Row text: {raw_text!r}")

            action_icons = row.locator(
                "[data-testid*='Sentiment'], button, [role='button']"
            ).count()
            log.info(f"  Clickable action icons in row: {action_icons}")

            url_before = page.url
            row.locator("td").first.click()
            _pause(2.0, 3.0)
            url_after = page.url

            safe = name.replace(" ", "_")
            snap(page, f"withdrawn_check_{safe}")

            navigated = (url_after != url_before)
            log.info(f"  URL before: {url_before}")
            log.info(f"  URL after:  {url_after}")
            log.info(f"  Navigated to a profile: {navigated}")

            # Recover back to the Rejected tab list for the next check
            if navigated:
                back = page.locator("[data-testid='BackToListButton']").first
                if back.count() > 0:
                    back.click()
                else:
                    page.go_back()
                _pause(1.5, 2.5)
            page.goto(tab_url, wait_until="domcontentloaded", timeout=30_000)
            _pause(1.5, 2.5)
            _dismiss_popup(page)
            _click_tab(page, TAB_NAME)
            page.wait_for_selector(_LIST_CONTAINER_SEL, timeout=20_000)

        browser.close()


if __name__ == "__main__":
    run()
